"""Многоступенчатое сопоставление публикаций с базовыми активами (полностью локальное).

Уровни надёжности:
  A — точные идентификаторы (ISIN/CUSIP/CIK/LEI, структурированный SEC filer);
  B — точные названия (полное имя, имя без юр. суффиксов, эмитент);
  C — алиасы (целые слова/фразы; короткие тикеры — только с контекстом);
  D — fuzzy (RapidFuzz; только как кандидат при наличии другого признака);
  E — источник (персональная IR-страница актива — сильный сигнал,
      глобальный SEC/Nasdaq-фид сам по себе — не сигнал).

Результат — MatchResult со скором, уровнем уверенности и объяснением.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

from app import db
from app.fetcher import plain_text
from app.versions import MATCHER_VERSION  # noqa: F401  (реэкспорт для остальных модулей)

log = logging.getLogger(__name__)

# Признаки корпоративного события. Публикация без единого совпадения в LLM не отправляется.
# Сопоставляются целые слова (с английскими словоформами: acquire → acquires/acquired/
# acquiring), русские — как основы слов. Подстрока не годится: «isin» ловило
# rising/advertising, «adr» — quadrant и base64-адреса Google News.
CORPORATE_KEYWORDS = [
    "split", "reverse split", "dividend", "special dividend", "record date", "ex-date",
    "ex date", "spin-off", "spinoff", "merger", "acquisition", "acquire", "rights issue",
    "shareholder return", "funding round",
    "adr", "isin", "cusip", "symbol change", "ticker change", "bankruptcy", "liquidation",
    "rebalance", "index change", "listing", "delisting", "contract specification",
    "trading suspension", "trading halt", "shareholder meeting", "sec filing",
    "8-k", "6-k", "13d", "13g", "tender offer", "buyback", "issuer bid",
    "redenomination", "conversion", "name change", "corporate action",
    "capital return", "capital distribution", "distribution", "consolidation", "share issuance",
    "дроблени", "делистинг", "слияни", "поглощен", "банкрот", "выкуп",
    "приостановка торгов", "спецификаци", "дивиденд",
    # криптоактивы: события сети и биржевые действия с монетой
    "hard fork", "hardfork", "halving", "network upgrade", "protocol upgrade", "mainnet",
    "airdrop", "token burn", "outage", "network halt", "chain halt", "restart", "delist",
    "хардфорк", "халвинг", "остановка сети", "обновление сети", "апгрейд",
    # товарные фьючерсы: гарантийное обеспечение, спецификации, условия обращения
    "initial margin", "margin requirement", "margin rate", "performance bond",
    "position limit", "circular", "amendment", "delivery month", "expiry",
    "settlement method", "final settlement", "гарантийн", "маржинальн",
    "futures margin", "margin change", "margin increase", "margin decrease", "margin cut",
    "margin hike", "raises margin", "lowers margin", "cuts margin", "hikes margin",
    "price limit", "price band", "daily limit", "trading hours", "last trading day",
    "delivery point", "delivery rules", "contract size", "tick size", "contract launch",
    "new contract", "cease trading", "price auction", "benchmark change", "rulebook",
    "exchange notice", "notice to members", "лимит позиц", "ценовой лимит",
    "часы торгов", "последний день торгов", "новый контракт",
    # Японские раскрытия TSE/TDnet (Toyota): выкуп, тендер, сплит, дивиденд, M&A, листинг.
    "自己株式", "自社株", "公開買付", "株式分割", "株式併合", "配当", "合併", "株式交換",
    "株式移転", "会社分割", "上場廃止", "上場", "買収", "商号変更", "第三者割当", "消却",
    "売買停止", "株主総会", "減資", "売出し",
    # Корейские раскрытия DART/KRX (Samsung): тот же набор событий. Рутинные отчёты
    # о владении бумагами руководством ни одного из этих слов не содержат.
    "자기주식", "자사주", "공개매수", "주식분할", "액면분할", "주식병합", "배당", "합병",
    "분할", "상장폐지", "상장", "주식교환", "유상증자", "무상증자", "감자결정", "주식소각",
    "매매거래정지", "거래정지", "주주총회", "상호변경", "타법인주식", "영업양수", "영업양도",
]

# Японский и корейский пишутся без пробелов: термин внутри заголовка ищется подстрокой.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")

# Слова, которые для акций слишком общие («analyst upgrade», «cash burn»), но для своего
# типа актива означают событие: обновление сети, сжигание токенов, остановка вывода
# на бирже, склады LME. Засчитываются только в заголовке и только если публикация
# уверенно (high/medium) сопоставлена с активом этого типа.
ASSET_TYPE_KEYWORDS = {
    "CRYPTO": ["upgrade", "activation", "burn", "withdrawal"],
    "ASSET": ["warehouse"],
}


def _keyword_pattern(keyword: str) -> str:
    stem = re.escape(keyword)
    if _CJK_RE.search(keyword):
        return stem
    if re.search(r"[а-яё]", keyword):
        return rf"(?<!\w){stem}"
    forms = [stem + r"(?:s|es|d|ed|ing)?"]
    if keyword.endswith("e"):
        forms.append(re.escape(keyword[:-1]) + "ing")
    if re.search(r"[^aeiou][aeiou][bdgklmnprt]$", keyword):
        forms.append(stem + re.escape(keyword[-1]) + "(?:ing|ed)")   # split → splitting
    return rf"(?<!\w)(?:{'|'.join(forms)})(?!\w)"


def _keywords_re(keywords: list[str]) -> re.Pattern:
    return re.compile("|".join(_keyword_pattern(k) for k in keywords))


_CORPORATE_RE = _keywords_re(CORPORATE_KEYWORDS)
_ASSET_TYPE_RES = {t: _keywords_re(ks) for t, ks in ASSET_TYPE_KEYWORDS.items()}

# Контекст, легализующий короткий/неоднозначный тикер (SPY, EM, GOLD, DAX…).
TICKER_CONTEXT_WORDS = {"etf", "fund", "trust", "shares", "ticker", "symbol", "nyse",
                        "nasdaq", "etn", "adr", "dividend", "distribution", "listing", "index",
                        "crypto", "cryptocurrency", "blockchain", "token", "coin", "network",
                        "mainnet", "protocol", "exchange", "binance", "coinbase", "kraken",
                        "futures", "margin", "lme", "cme", "lbma", "contract", "commodity",
                        "bullion"}

# Тикеры, совпадающие с обычными словами: засчитываются только в скобках «(COIN)»,
# после «NASDAQ:/NYSE:/ticker/symbol» либо на своей странице — общий контекст не спасает.
STRICT_TICKERS = {"coin", "app", "dash", "lite", "hood", "uber", "cmg", "lulu", "spcx",
                  "on", "u", "be", "now", "arm", "team", "snow", "spot", "shop"}
_TICKER_PREFIX_RE = r"(?:nasdaq|nyse|ticker|symbol|тикер)\s*:?\s*"

# Рутинные формы SEC, которые корпоративным событием не бывают (сделки инсайдеров,
# уведомления о продаже, отчёты фондов о позициях, планы опционов). 8-K/6-K/25/497/
# DEF 14A/S-4/SC TO и прочие — проходят.
ROUTINE_SEC_FORMS = {"3", "3/A", "4", "4/A", "5", "5/A", "144", "144/A", "13F-HR", "13F-HR/A",
                     "13F-NT", "SC 13G", "SC 13G/A", "SC 13D", "SC 13D/A", "S-8", "S-8 POS",
                     "11-K", "NPORT-P", "NPORT-EX", "N-PX", "ARS", "D", "D/A", "SD", "PX14A6G"}

# Похоже на биржевой тикер: цифры/точка внутри ("0700.hk", "2330.tw", "2800 hk").
_TICKERISH_RE = re.compile(r"^[a-z0-9][a-z0-9. \-]{1,11}$")

# Заведомо неоднозначные тикеры: без контекста не сопоставляются вовсе.
AMBIGUOUS_TICKERS = {"spy", "em", "br", "ng", "plt", "pld", "gold", "silv", "dax", "sol",
                     "sugr", "ttf", "china", "india", "brazil", "cocoa", "coffee", "orange"}

# Общие слова провайдеров/юр. форм: сами по себе дают не более WEIGHT_PROVIDER.
GENERIC_WORDS = {"global", "market", "index", "fund", "trust", "group", "holdings",
                 "capital", "financial", "international", "select", "core", "spdr",
                 "ishares", "invesco", "vaneck", "xtrackers", "state street", "etf"}

# Веса уровней.
W_EXACT_ID = 1.00
W_FULL_NAME = 0.90
W_NORM_NAME = 0.85
W_ISSUER = 0.85
W_TICKER_CTX = 0.90
W_TICKER_BARE = 0.35
W_LONG_ALIAS = 0.70
W_MID_ALIAS = 0.55
W_OWNED_BONUS = 0.45
W_FUZZY_HIGH = 0.45     # ≥ 92
W_FUZZY_MID = 0.25      # 85–92
W_PROVIDER = 0.20
NEGATIVE_PENALTY = 0.30

HIGH, MEDIUM, LOW = 0.80, 0.60, 0.40

# Фонды/индексы: их «свои» страницы (биржа, сайт фонда) перечисляют новости компаний
# из состава индекса. Такой публикации без слов о самом фонде фонд не получает.
FUND_TYPES = {"ETF", "INDEX", "FUND"}
FUND_CONTEXT_WORDS = {"fund", "etf", "trust", "unit", "units", "unitholder", "unitholders",
                      "shareholders of the fund", "nav", "index", "rebalance", "rebalancing",
                      "constituent", "distribution", "creation", "redemption", "фонд", "паи", "индекс"}
# Признаки, означающие, что актив назван в тексте сам (а не выведен из источника).
NAMED_SIGNALS = {"exact_isin", "exact_cusip", "exact_cik", "exact_lei", "exact_full_name",
                 "exact_normalized_name", "exact_issuer_name", "long_alias", "ticker_with_context",
                 "manual_rule", "sec_cik_exact", "sec_filer_exact_name"}

_WORD_RE = re.compile(r"\w+", re.UNICODE)


# --- Хэши и базовые фильтры ---------------------------------------------------

def url_hash(url: str) -> str:
    normalized = (url or "").strip().lower().rstrip("/")
    normalized = re.sub(r"^https?://(www\.)?", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def title_hash(title: str) -> str | None:
    words = _WORD_RE.findall((title or "").lower())
    if not words:
        return None
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()


def has_corporate_keywords(publication: dict) -> bool:
    text = plain_text(f"{publication.get('title', '')} {publication.get('text', '')}").lower()
    return _CORPORATE_RE.search(text) is not None


def keyword_gate(publication: dict, profiles: list["AssetProfile"]) -> bool:
    """Keyword-фильтр перед LLM: общие признаки события либо слово своего типа
    актива в заголовке при уверенном сопоставлении с активом этого типа."""
    if has_corporate_keywords(publication):
        return True
    title = plain_text(publication.get("title")).lower()
    types = {t for t, rx in _ASSET_TYPE_RES.items() if rx.search(title)}
    if not types:
        return False
    by_code = {p.code: p.instrument_type for p in profiles}
    return any(r.confidence in ("high", "medium") and by_code.get(r.asset_code) in types
               for r in match_publication(publication, profiles))


def _norm(text: str) -> str:
    text = (text or "").lower().replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def _phrase_in(phrase: str, text: str) -> bool:
    """Точная фраза по границам токенов (SPY не совпадает с частью слова)."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


# --- Профили активов -----------------------------------------------------------

@dataclass
class AssetProfile:
    code: str
    instrument_type: str
    full_name: str
    ids: dict[str, str] = field(default_factory=dict)   # isin/cusip/cik/lei → значение
    names: list[str] = field(default_factory=list)      # (фраза, полная/нормализованная)
    issuer: str | None = None
    long_aliases: list[str] = field(default_factory=list)
    mid_aliases: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    rule_aliases: list[tuple[str, float]] = field(default_factory=list)


_SUFFIX_RE = re.compile(
    r"\s+(etf trust|ucits etf|etf|trust|ltd|inc|corp|corporation|adr|plc|fund|futures|index)\.?$")


def _strip_suffixes(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = _SUFFIX_RE.sub("", name).strip(" ,–-")
    return name


def build_profiles(assets: list[dict], rules: list[dict] | None = None) -> list[AssetProfile]:
    rules_by_asset: dict[str, list[dict]] = {}
    for r in rules or []:
        rules_by_asset.setdefault(r["asset_code"], []).append(r)
    profiles = []
    for a in assets:
        p = AssetProfile(code=a["code"], instrument_type=a.get("instrument_type", ""),
                         full_name=a["name"])
        for key in ("isin", "cusip", "lei"):
            if a.get(key):
                p.ids[key] = str(a[key]).strip().upper()
        if a.get("cik"):
            p.ids["cik"] = str(int(str(a["cik"]).strip() or 0))
        name = _norm(a["name"])
        p.names.append(name)
        no_paren = _norm(re.sub(r"\s*\([^)]*\)\s*$", "", a["name"]))
        if len(no_paren) >= 8 and no_paren != name:
            p.names.append(no_paren)
        stripped = _strip_suffixes(no_paren)
        if len(stripped) >= 8 and stripped not in p.names:
            p.names.append(stripped)
        if a.get("issuer_name"):
            p.issuer = _norm(a["issuer_name"])
        for t in (a.get("underlying_ticker"), a.get("adr_ticker"), a.get("local_ticker")):
            if t and _norm(t) not in p.tickers:
                p.tickers.append(_norm(t))
        for alias in a.get("aliases", []):
            alias_n = _norm(alias)
            if not alias_n or alias_n in p.names or alias_n in p.tickers:
                continue
            if alias_n in GENERIC_WORDS:
                continue
            tickerish = len(alias_n) <= 12 and _TICKERISH_RE.fullmatch(alias_n) and (
                any(ch.isdigit() for ch in alias_n) or "." in alias_n or len(alias_n) <= 4)
            if tickerish:
                p.tickers.append(alias_n)   # биржевые тикеры — по правилам тикеров
            elif len(alias_n) >= 8:
                p.long_aliases.append(alias_n)
            elif len(alias_n) >= 5:
                p.mid_aliases.append(alias_n)
            else:
                p.tickers.append(alias_n)
        for r in rules_by_asset.get(a["code"], []):
            if r["rule_type"] == "negative":
                p.negatives.append(_norm(r["pattern"]))
            elif r["rule_type"] == "alias":
                p.rule_aliases.append((_norm(r["pattern"]), float(r["weight"] or W_LONG_ALIAS)))
        profiles.append(p)
    # Первое слово имени — самостоятельный длинный алиас, только если оно
    # характерное (не общее слово) и уникально среди активов: «alibaba», «tencent».
    first_words: dict[str, int] = {}
    for p in profiles:
        stripped = _strip_suffixes(p.names[-1] if p.names else "")
        word = stripped.split()[0] if stripped else ""
        if len(word) >= 5 and word not in GENERIC_WORDS:
            first_words[word] = first_words.get(word, 0) + 1
            p._first_word = word  # type: ignore[attr-defined]
    for p in profiles:
        word = getattr(p, "_first_word", "")
        if word and first_words.get(word) == 1 and word not in p.long_aliases:
            p.long_aliases.append(word)
    return profiles


# --- Результат сопоставления ---------------------------------------------------

@dataclass
class MatchResult:
    asset_code: str
    score: float
    confidence: str                 # high | medium | low | none
    matched_by: list[str]
    matched_terms: list[str]
    rejected_terms: list[str]
    explanation: str

    def as_dict(self) -> dict:
        return {"contract_code": self.asset_code, "score": round(self.score, 2),
                "confidence": self.confidence, "matched_by": self.matched_by,
                "matched_terms": self.matched_terms, "rejected_terms": self.rejected_terms,
                "explanation": self.explanation}


def _confidence(score: float) -> str:
    if score >= HIGH:
        return "high"
    if score >= MEDIUM:
        return "medium"
    if score >= LOW:
        return "low"
    return "none"


def _source_owned(pub: dict, code: str) -> bool:
    """Источник персональный: на него подписан этот актив и подписчиков мало.
    Глобальные фиды (SEC/Nasdaq, без подписчиков) владения не дают.
    Поисковые результаты (Tavily) — тоже: то, что мы искали по активу,
    не значит, что найденное о нём; для SEARCH обязательно текстовое совпадение."""
    if pub.get("source_type") in ("SEARCH", "NEWS"):
        return False  # поиск и новостные агрегаторы (Tavily, Google News) — не «свои» страницы
    subscribers = pub.get("subscribers") or []
    return code in subscribers and 0 < len(subscribers) <= 2


def _searched_for_asset(pub: dict, code: str) -> bool:
    """Публикация из поисковой ленты, собранной по запросу этого актива
    (персональная лента Google News или Tavily-поиск по активу)."""
    subscribers = pub.get("subscribers") or []
    return (pub.get("source_type") in ("SEARCH", "NEWS")
            and code in subscribers and 0 < len(subscribers) <= 2)


def _match_sec(pub: dict, profile: AssetProfile) -> MatchResult | None:
    """Публикации SEC-фидов сопоставляются только по filer: CIK либо точное имя.
    Никакого fuzzy и никакого владения источником."""
    filer = _norm(pub.get("sec_filer") or "")
    cik = str(pub.get("sec_cik") or "").lstrip("0")
    if cik and profile.ids.get("cik") == cik:
        return MatchResult(profile.code, W_EXACT_ID, "high", ["sec_cik_exact"],
                           [cik], [], f"CIK {cik} совпал с {profile.code}")
    if filer:
        candidates = [*profile.names, profile.issuer or "", *profile.long_aliases]
        stripped_filer = _strip_suffixes(filer)
        for name in candidates:
            if name and (filer == name or stripped_filer == _strip_suffixes(name)):
                return MatchResult(profile.code, W_FULL_NAME, "high", ["sec_filer_exact_name"],
                                   [pub.get("sec_filer", "")], [],
                                   f"Имя filer «{pub.get('sec_filer')}» совпало с {profile.code}")
    return None


def match_one(pub: dict, profile: AssetProfile) -> MatchResult:
    """Скоринг публикации против одного актива."""
    if pub.get("sec_cik") or pub.get("sec_filer"):
        result = _match_sec(pub, profile)
        return result or MatchResult(profile.code, 0.0, "none", [], [], [],
                                     "SEC-публикация: filer не относится к активу")

    text = _norm(f"{pub.get('title', '')} {pub.get('text', '')}")
    matched_by: list[str] = []
    matched_terms: list[str] = []
    rejected_terms: list[str] = []
    signals: list[float] = []

    for kind, value in profile.ids.items():
        if value and _phrase_in(value.lower(), text):
            signals.append(W_EXACT_ID)
            matched_by.append(f"exact_{kind}")
            matched_terms.append(value)
    for i, name in enumerate(profile.names):
        if _phrase_in(name, text):
            signals.append(W_FULL_NAME if i == 0 else W_NORM_NAME)
            matched_by.append("exact_full_name" if i == 0 else "exact_normalized_name")
            matched_terms.append(name)
            break
    if profile.issuer and _phrase_in(profile.issuer, text):
        signals.append(W_ISSUER)
        matched_by.append("exact_issuer_name")
        matched_terms.append(profile.issuer)
    for alias in profile.long_aliases:
        if _phrase_in(alias, text):
            signals.append(W_LONG_ALIAS)
            matched_by.append("long_alias")
            matched_terms.append(alias)
            break
    for alias in profile.mid_aliases:
        if _phrase_in(alias, text):
            signals.append(W_MID_ALIAS)
            matched_by.append("mid_alias")
            matched_terms.append(alias)
            break
    for pattern, weight in profile.rule_aliases:
        if _phrase_in(pattern, text):
            signals.append(weight)
            matched_by.append("manual_rule")
            matched_terms.append(pattern)
            break

    owned = _source_owned(pub, profile.code)
    has_context = any(_phrase_in(w, text) for w in TICKER_CONTEXT_WORDS)
    for ticker in profile.tickers:
        if not _phrase_in(ticker, text):
            continue
        parenthesized = re.search(rf"\({re.escape(ticker)}\)", text) is not None \
            or re.search(_TICKER_PREFIX_RE + re.escape(ticker) + r"(?!\w)", text) is not None
        # «NASDAQ: ON», «NYSE: U» — однозначная ссылка на бумагу при любой длине тикера.
        prefixed = re.search(_TICKER_PREFIX_RE + re.escape(ticker) + r"(?!\w)", text) is not None
        strict = ticker in STRICT_TICKERS
        context = parenthesized or owned or bool(matched_terms) or (has_context and not strict)
        if strict and not context:
            rejected_terms.append(ticker)
            continue
        if ticker in AMBIGUOUS_TICKERS and not context:
            rejected_terms.append(ticker)
            continue
        if context:
            signals.append(W_TICKER_CTX if len(ticker) >= 3 or prefixed else W_MID_ALIAS)
            matched_by.append("ticker_with_context")
        else:
            signals.append(W_TICKER_BARE)
            matched_by.append("ticker_no_context")
        matched_terms.append(ticker)
        break

    # Уровень D: fuzzy — только кандидат, требует любого другого признака.
    fuzzy_score = fuzz.token_set_ratio(_norm(pub.get("title", "")), profile.names[0]) if profile.names else 0
    if fuzzy_score >= 85 and (signals or owned):
        signals.append(W_FUZZY_HIGH if fuzzy_score >= 92 else W_FUZZY_MID)
        matched_by.append(f"fuzzy_{fuzzy_score:.0f}")

    score = max(signals) if signals else 0.0
    if owned and not signals and profile.instrument_type in FUND_TYPES \
            and not any(_phrase_in(w, text) for w in FUND_CONTEXT_WORDS):
        # Страница фонда/биржи, но в тексте ни слова о фонде — это новость
        # о компании из состава индекса, владение не засчитываем.
        owned = False
        rejected_terms.append("source_owned:constituent_news")
    if owned and (signals or has_corporate_keywords(pub)):
        score += W_OWNED_BONUS
        matched_by.append("source_owned_by_asset")
        # Персональная IR-страница + признаки корпоративного события —
        # достаточное основание для medium даже без текстового совпадения имени.
        if not signals:
            score = max(score, MEDIUM)
    strong = [s for s in signals if s >= W_MID_ALIAS]
    if len(strong) >= 2:
        score += 0.05
    # Собственная поисковая лента актива (Google News / Tavily по его запросу) и имя
    # или бренд в тексте («Tesla announces stock split»): одного бренда для другого
    # источника мало (low), но здесь публикацию искали именно по этому активу —
    # достаточно для medium, окончательно решает Gemini. Текстовое совпадение
    # обязательно: сама по себе лента владения не даёт.
    if not owned and score < MEDIUM and strong and _searched_for_asset(pub, profile.code):
        score = MEDIUM
        matched_by.append("asset_news_feed")

    for negative in profile.negatives:
        if _phrase_in(negative, text) and score < W_FULL_NAME and not owned:
            score -= NEGATIVE_PENALTY
            rejected_terms.append(negative)
            matched_by.append("negative_alias_penalty")

    score = max(0.0, min(1.0, score))
    explanation = (f"{profile.code}: score {score:.2f} — "
                   + ("; ".join(matched_by) if matched_by else "совпадений нет"))
    return MatchResult(profile.code, score, _confidence(score),
                       matched_by, matched_terms, rejected_terms, explanation)


def match_publication(pub: dict, profiles: list[AssetProfile]) -> list[MatchResult]:
    """Все ненулевые совпадения, отсортированные по score.

    Взаимоисключение: если публикация по имени принадлежит компании (акция/ADR),
    фонды и индексы, не названные в тексте сами, её не получают — новость об Alibaba
    не относится к Tracker Fund, даже если пришла со страницы биржи."""
    types = {p.code: p.instrument_type for p in profiles}
    results = [match_one(pub, p) for p in profiles]
    results = [r for r in results if r.score > 0]
    company_named = any(types.get(r.asset_code) not in FUND_TYPES
                        and set(r.matched_by) & NAMED_SIGNALS for r in results)
    if company_named:
        results = [r for r in results
                   if types.get(r.asset_code) not in FUND_TYPES
                   or set(r.matched_by) & NAMED_SIGNALS]
    results.sort(key=lambda r: r.score, reverse=True)
    return results


# Обратная совместимость для простых проверок.
def match_assets(publication: dict, profiles: list[AssetProfile],
                 subscribers: list[str] | None = None) -> list[str]:
    if subscribers is not None:
        publication = {**publication, "subscribers": subscribers}
    return [r.asset_code for r in match_publication(publication, profiles)
            if r.confidence in ("high", "medium")]


def build_asset_index(assets: list[dict]) -> list[AssetProfile]:
    """Совместимое имя: теперь возвращает профили для скоринга."""
    return build_profiles(assets, db.list_matcher_rules())


# --- Конвейерная фильтрация -----------------------------------------------------

def filter_publications(publications: list[dict], profiles: list[AssetProfile],
                        lookback_days: int, stats=None) -> list[dict]:
    """Каскад дешёвых фильтров + скоринг. На выходе — публикации с asset_codes
    (high/medium), url_hash, title_hash и match_results. Low/unmatched сохраняются
    в очередь диагностики и не идут в LLM."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    kept: list[dict] = []
    for p in publications:
        uh, th = url_hash(p.get("url", "")), title_hash(p.get("title", ""))
        if uh in seen_urls or (th and th in seen_titles):
            continue
        seen_urls.add(uh)
        if th:
            seen_titles.add(th)
        published = p.get("published_dt")
        if published and published < cutoff:
            continue
        if db.article_seen(uh, th):
            if stats is not None:
                stats.cache_hits += 1
            continue
        if stats is not None:
            stats.new_after_cache += 1
        # Регуляторные филинги (SEC: 8-K, 6-K, Form 25, 497…) — корпоративные документы
        # по определению: keyword-фильтр по заголовку к ним неприменим (в заголовке
        # только код формы), решение принимают сопоставление по CIK и LLM.
        is_regulatory = bool(p.get("sec_cik") or p.get("sec_filer") or p.get("form_type"))
        if is_regulatory and str(p.get("form_type") or "").strip().upper() in ROUTINE_SEC_FORMS:
            db.remember_article(p.get("url", ""), uh, th, p.get("published_at"),
                                p.get("source"), "skipped:routine_sec_form",
                                status="keyword_filtered", title=p.get("title"),
                                source_id=p.get("source_id"))
            continue
        if not is_regulatory and not keyword_gate(p, profiles):
            db.remember_article(p.get("url", ""), uh, th, p.get("published_at"),
                                p.get("source"), "skipped:no_keywords",
                                status="keyword_filtered", title=p.get("title"),
                                source_id=p.get("source_id"))
            continue
        if stats is not None:
            stats.after_keywords += 1
        results = match_publication(p, profiles)
        best = results[0] if results else None
        confident = [r for r in results if r.confidence in ("high", "medium")]
        if confident:
            if stats is not None:
                stats.matched_high += sum(1 for r in confident if r.confidence == "high")
                stats.matched_medium += sum(1 for r in confident if r.confidence == "medium")
            kept.append({**p, "url_hash": uh, "title_hash": th,
                         "asset_codes": [r.asset_code for r in confident],
                         "match_results": confident})
            continue
        # Чужие публикации глобальных SEC/биржевых фидов и поисковый шум Tavily
        # без единого кандидата не засоряют пользовательскую очередь unmatched.
        is_global_feed = (not (p.get("subscribers") or [])
                          or p.get("source_type") in ("SEARCH", "NEWS"))
        if is_global_feed and not results:
            if stats is not None:
                stats.irrelevant_global += 1
            db.remember_article(p.get("url", ""), uh, th, p.get("published_at"),
                                p.get("source"), "skipped:irrelevant_global",
                                status="irrelevant_global", title=p.get("title"), text=p.get("text"),
                                source_id=p.get("source_id"))
            continue
        # Low и unmatched со слабым кандидатом: локальная очередь диагностики, в LLM не идут.
        reason = f"best {best.score:.2f} ({best.asset_code})" if best else "no_candidates"
        if best and best.confidence == "low" and stats is not None:
            stats.matched_low += 1
        elif stats is not None:
            stats.unmatched += 1
        db.remember_article(p.get("url", ""), uh, th, p.get("published_at"),
                            p.get("source"), f"skipped:no_asset_match ({reason})",
                            status="unmatched", title=p.get("title"), text=p.get("text"),
                            best_asset=best.asset_code if best else None,
                            best_score=best.score if best else None,
                            source_id=p.get("source_id"))
        db.save_unmatched(uh, best.asset_code if best else None,
                          best.score if best else None,
                          json.dumps([r.as_dict() for r in results[:5]], ensure_ascii=False),
                          "low_confidence" if best and best.confidence == "low" else "no_match")
    return kept
