"""Синхронизация активов и источников из underlyings_sources.md (рабочая методология).

Файл — источник полноты: при каждом старте сервиса недостающие активы, ISIN,
биржи и источники добавляются в SQLite. Существующие данные не перезаписываются
и не удаляются (активы, которых нет в файле, остаются под мониторингом —
они попадают в отчёт синхронизации как «лишние»). Операция идемпотентна.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app import db
from app.matcher import STRICT_TICKERS

log = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^##\s+(?P<code>.+?)\s*$")
_FIELD_RE = re.compile(r"^\-\s+\*\*(?P<key>[^:*]+):\*\*\s*(?P<value>.*)$")
_URL_RE = re.compile(r"^\s*-\s+(?P<url>https?://\S+)\s*$")
_TICKER_FROM_URL_RE = re.compile(r"nasdaq\.com/market-activity/(?:stocks|etf)/([a-z0-9.]+)/", re.I)


def parse_underlyings_md(path: str | Path) -> list[dict]:
    """Разбирает markdown в список активов: code, name, isin, exchange, sources."""
    assets: list[dict] = []
    current: dict | None = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = {"code": m.group("code").strip(), "name": "", "isin": None,
                       "exchange": None, "sources": [], "cik": None, "issuer": None, "aliases": [],
                       "ticker": None}
            assets.append(current)
            continue
        if current is None:
            continue
        f = _FIELD_RE.match(line.strip())
        if f:
            key, value = f.group("key").strip().lower(), f.group("value").strip()
            if key == "базовый актив":
                current["name"] = value
            elif key == "isin":
                current["isin"] = value.strip("`").strip() or None
            elif key.startswith("биржа"):
                current["exchange"] = value or None
            elif key == "cik":
                current["cik"] = value.strip("`").strip() or None
            elif key == "эмитент":
                current["issuer"] = value or None
            elif key == "тикер":
                current["ticker"] = value.strip("`").strip().upper() or None
            elif key == "алиасы":
                current["aliases"] = [a.strip() for a in value.split(",") if a.strip()]
            continue
        u = _URL_RE.match(line)
        if u and current is not None:
            url = u.group("url").strip()
            if url not in current["sources"]:
                current["sources"].append(url)
    return [a for a in assets if a["name"]]


# Криптоактивы (индексы МосБиржи на монеты): «эмитент» индекса — сама монета.
# issuer_name даёт matcher'у точное совпадение по имени сети, тикер — по коду.
CRYPTO_PROFILES = {
    "BTC": {"issuer": "Bitcoin", "aliases": ["Биткоин", "MOEXBTC"], "query": '"Bitcoin" OR BTC'},
    "ETH": {"issuer": "Ethereum", "aliases": ["Ether", "Эфир", "Эфириум", "MOEXETH"], "query": '"Ethereum" OR ETH'},
    "SOL": {"issuer": "Solana", "aliases": ["Солана", "MOEXSOL"], "query": '"Solana" OR SOL'},
    "XRP": {"issuer": "XRP Ledger", "aliases": ["Ripple", "Рипл", "MOEXXRP"], "query": '"XRP" OR Ripple'},
    "TRX": {"issuer": "TRON", "aliases": ["Трон", "TRON network", "MOEXTRX"], "query": '"TRON" OR TRX'},
    "BNB": {"issuer": "BNB Chain", "aliases": ["Binance Coin", "Бинанс коин", "MOEXBNB"], "query": '"BNB" OR "BNB Chain"'},
}
_CRYPTO_TERMS = '("hard fork" OR upgrade OR halving OR outage OR delisting OR "network halt" OR burn)'

# Товарные активы: «эмитент» контракта — сам товар. Имя товара (copper, gold…) —
# единственное, по чему публикация биржи/СМИ сопоставляется с активом, и оно же
# совпадает с кодом контракта: как алиас оно терялось (UNIQUE(asset_code, identifier)
# NOCASE против записи kind='code'), поэтому задаётся как issuer_name — matcher
# считает совпадение по эмитенту сильным сигналом независимо от таблицы алиасов.
COMMODITY_PROFILES = {
    "ALUM": {"issuer": "Aluminium", "aliases": ["Aluminum", "LME Aluminium", "Алюминий"],
             "query": '(LME OR "London Metal Exchange") aluminium'},
    "COPPER": {"issuer": "Copper", "aliases": ["LME Copper", "Медь"],
               "query": '(LME OR "London Metal Exchange" OR COMEX) copper'},
    "ZINC": {"issuer": "Zinc", "aliases": ["LME Zinc", "Цинк"],
             "query": '(LME OR "London Metal Exchange") zinc'},
    "NICKEL": {"issuer": "Nickel", "aliases": ["LME Nickel", "Никель"],
               "query": '(LME OR "London Metal Exchange") nickel'},
    "BR, BRM": {"issuer": "Brent", "aliases": ["Brent Crude", "ICE Brent", "Брент"],
                "query": '"Brent" (ICE OR futures)'},
    "WTI": {"issuer": "WTI", "aliases": ["Crude Oil", "Light Sweet Crude", "NYMEX crude"],
            "query": '(WTI OR "crude oil") (CME OR NYMEX OR futures)'},
    "NG, NGM": {"issuer": "Henry Hub", "aliases": ["Natural Gas", "NYMEX natural gas"],
                "query": '"Henry Hub" OR "natural gas futures"'},
    "SUGR": {"issuer": "Sugar", "aliases": ["Sugar No. 11", "Sugar No.11", "raw sugar"],
             "query": '"Sugar No. 11" OR "raw sugar futures"'},
    "GOLD": {"issuer": "Gold", "aliases": ["Gold price", "LBMA gold", "COMEX gold", "Золото"],
             "query": '(LBMA OR COMEX OR CME) gold'},
    "SILV": {"issuer": "Silver", "aliases": ["Silver price", "LBMA silver", "COMEX silver", "Серебро"],
             "query": '(LBMA OR COMEX OR CME) silver'},
    "PLT": {"issuer": "Platinum", "aliases": ["LBMA platinum", "NYMEX platinum", "Платина"],
            "query": '(LBMA OR NYMEX OR CME) platinum'},
    "PLD": {"issuer": "Palladium", "aliases": ["LBMA palladium", "NYMEX palladium", "Палладий"],
            "query": '(LBMA OR NYMEX OR CME) palladium'},
    "COCOA": {"issuer": "Cocoa", "aliases": ["Cocoa Futures", "Какао"],
              "query": 'cocoa futures (ICE OR exchange)'},
    "COFFEE": {"issuer": "Coffee", "aliases": ["Coffee C", "Arabica", "Кофе"],
               "query": '"Coffee C" OR "arabica futures"'},
    "TTF": {"issuer": "Dutch TTF", "aliases": ["TTF gas", "Title Transfer Facility"],
            "query": '"TTF" gas (ICE OR Endex OR futures)'},
    "ORANGE": {"issuer": "Orange Juice", "aliases": ["FCOJ", "FCOJ-A", "frozen concentrated orange juice"],
               "query": '("orange juice" OR FCOJ) futures'},
}
_COMMODITY_TERMS = ('(margin OR margins OR "position limit" OR "price limit" OR specification '
                    'OR amendment OR circular OR notice OR delivery OR settlement OR "trading hours" '
                    'OR launch OR delisting OR suspension)')


def infer_instrument_type(name: str) -> str:
    lower = name.lower()
    if "депозитарные расписки" in lower:
        return "ADR"
    if lower.startswith("акции"):
        return "SHARE"
    if any(w in lower for w in ("etf", "ucits", "trust", "фонд")):
        return "ETF"
    return "ASSET"


def english_alias(name: str) -> str | None:
    """Из «Американские депозитарные расписки на акции Alibaba Group Holding Limited»
    достаёт английскую часть — она нужна matcher'у для сопоставления с публикациями."""
    m = re.search(r"(?:на акции|^Акции)\s+(.+)$", name, re.I)
    if m:
        candidate = m.group(1).strip()
        return candidate if len(candidate) >= 5 else None
    return None


def sync_from_md(path: str | Path) -> dict:
    """Добавляет недостающее из md в SQLite. Возвращает отчёт синхронизации."""
    parsed = parse_underlyings_md(path)
    existing = {a["code"]: a for a in db.list_assets()}
    report = {"assets_added": [], "isin_filled": [], "sources_added": 0,
              "aliases_added": [], "extra_in_db": []}

    for item in parsed:
        code = item["code"]
        asset = existing.get(code)
        if asset is None:
            itype = "CRYPTO" if code in CRYPTO_PROFILES else infer_instrument_type(item["name"])
            db.upsert_asset(code, item["name"], itype)
            report["assets_added"].append(code)
        if code in CRYPTO_PROFILES:
            prof = CRYPTO_PROFILES[code]
            fresh = db.get_asset(code) or {}
            if not fresh.get("issuer_name"):
                db.set_asset_field(code, "issuer_name", prof["issuer"])
            if not fresh.get("underlying_ticker"):
                db.set_asset_field(code, "underlying_ticker", code)
            for alias in [prof["issuer"], *prof["aliases"]]:
                if db.add_alias(code, alias):
                    report["aliases_added"].append(f"{code}:{alias}")
        elif code in COMMODITY_PROFILES:
            prof = COMMODITY_PROFILES[code]
            fresh = db.get_asset(code) or {}
            if not fresh.get("issuer_name"):
                db.set_asset_field(code, "issuer_name", prof["issuer"])
            for alias in prof["aliases"]:
                if db.add_alias(code, alias):
                    report["aliases_added"].append(f"{code}:{alias}")
        # Точные идентификаторы: заполняем только пустые, ничего не перезаписываем.
        current_isin = (asset or {}).get("isin")
        if item["isin"] and not current_isin:
            db.set_asset_field(code, "isin", item["isin"])
            report["isin_filled"].append(code)
        if item["exchange"] and not (asset or {}).get("exchange"):
            db.set_asset_field(code, "exchange", item["exchange"])
        if item.get("cik") and not (asset or {}).get("cik"):
            db.set_asset_field(code, "cik", item["cik"])
        if item.get("issuer") and not (asset or {}).get("issuer_name"):
            db.set_asset_field(code, "issuer_name", item["issuer"])
        # Биржевой тикер акции. Как алиас он теряется, если совпадает с кодом актива
        # (UNIQUE NOCASE против записи kind='code'), поэтому задаётся отдельным полем.
        if item.get("ticker") and not (db.get_asset(code) or {}).get("underlying_ticker"):
            db.set_asset_field(code, "underlying_ticker", item["ticker"])
        for extra in item.get("aliases", []):
            if db.add_alias(code, extra):
                report["aliases_added"].append(f"{code}:{extra}")
        # Английское имя эмитента из русского описания — как алиас для matcher.
        alias = english_alias(item["name"])
        if alias and db.add_alias(code, alias):
            report["aliases_added"].append(f"{code}:{alias}")
        # Тикеры, безопасно выводимые из URL источников (nasdaq.com/.../stocks/tsm/):
        # и как алиас, и как underlying_ticker (если он ещё не заполнен) —
        # иначе matcher не увидит тикер, совпадающий с кодом контракта.
        for url in item["sources"]:
            m = _TICKER_FROM_URL_RE.search(url)
            if not m:
                continue
            ticker = m.group(1).upper()
            if db.add_alias(code, ticker):
                report["aliases_added"].append(f"{code}:{ticker}")
            fresh = db.get_asset(code) if asset is None else asset
            if not (fresh or {}).get("underlying_ticker"):
                db.set_asset_field(code, "underlying_ticker", ticker)
        # Источники: реестр + подписка (идемпотентно по URL).
        known_urls = {s["url"] for s in (asset or {}).get("sources", [])}
        for url in item["sources"]:
            if url not in known_urls:
                db.add_source(code, url)
                report["sources_added"] += 1

    md_codes = {a["code"] for a in parsed}
    report["extra_in_db"] = sorted(set(existing) - md_codes)
    if report["assets_added"] or report["sources_added"] or report["isin_filled"]:
        log.info("Синхронизация с underlyings_sources.md: активов добавлено %d, "
                 "ISIN заполнено %d, источников добавлено %d",
                 len(report["assets_added"]), len(report["isin_filled"]), report["sources_added"])
    if report["extra_in_db"]:
        log.info("В базе есть активы, отсутствующие в md (остаются под мониторингом): %s",
                 ", ".join(report["extra_in_db"]))
    return report


# --- Резервные источники: каналы, которые по устройству не блокируют ботов -------

_GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}+when:3d&hl=en-US&gl=US&ceid=US:en"
# Признак автоматически созданной резервной ленты: только такие переписываются при смене
# запроса. Ленты из методологии (TRON DAO, site:lme.com — окно 7d) остаются как есть,
# иначе каждый старт переписывал бы их, а синхронизация добавляла бы обратно.
_AUTO_FEED_MARK = "+when:3d&"
_SEC_BY_CIK = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
               "&type=&dateb=&owner=include&count=40&output=atom")
_CORP_TERMS = "(split OR dividend OR delisting OR merger OR acquisition OR listing OR spin-off OR tender OR ADR)"


_COMMODITY_TERMS = ('(margin OR "position limit" OR contract OR specification '
                    'OR delivery OR settlement OR notice)')


def _news_query(asset: dict) -> str:
    """Запрос Google News: для бумаг — точное имя + тикер + корпоративные термины;
    для сырья (ASSET) — простой запрос по названию контракта + биржевые термины
    (корпоративные термины для LME/CME неуместны и дают пустую выдачу)."""
    from urllib.parse import quote_plus
    name = re.sub(r"\s*\([^)]*\)\s*$", "", asset["name"]).strip()
    name = re.sub(r"^(Американские депозитарные расписки на акции|Акции)\s+", "", name, flags=re.I)
    if asset.get("instrument_type") == "CRYPTO" and asset["code"] in CRYPTO_PROFILES:
        return quote_plus(f"{CRYPTO_PROFILES[asset['code']]['query']} {_CRYPTO_TERMS}")
    if asset["code"] in COMMODITY_PROFILES:
        return quote_plus(f"{COMMODITY_PROFILES[asset['code']]['query']} {_COMMODITY_TERMS}")
    if asset.get("instrument_type") == "ASSET":
        # «LBMA precious metal prices (…)» слишком длинно — берём до запятой/скобки
        short = re.split(r"[,(]", name)[0].strip()
        short = short.replace(" futures", "").replace(" Futures", "").replace(" Index", "")
        return quote_plus(f"{short} futures {_COMMODITY_TERMS}")
    parts = [f'"{name}"']
    ticker = asset.get("underlying_ticker")
    if ticker and ticker.lower() in STRICT_TICKERS:
        # Тикер-слово (NOW, SNOW, SHOP, COIN…) засоряет выдачу посторонними новостями —
        # вместо него ищем по короткому имени компании (первый алиас).
        alias = next((a for a in asset.get("aliases", []) if len(a) >= 4
                      and a.lower() != ticker.lower() and "(" not in a), None)
        if alias:
            parts.append(f'"{alias}"')
    elif ticker and len(ticker) >= 3:
        parts.append(ticker)
    return quote_plus(" OR ".join(parts) + " " + _CORP_TERMS)


def ensure_backup_sources() -> dict:
    """Для каждого актива гарантирует резервные каналы:
    - SEC EDGAR по CIK (официальные филинги эмитента/фонда) — если CIK известен;
    - Google News RSS по имени и тикеру — для всех.
    Идемпотентно: существующие источники не трогаются; при смене текста запроса
    Google News лента актива обновляется на месте, а не дублируется."""
    report = {"sec_added": 0, "news_added": 0, "news_updated": 0}
    for asset in db.list_assets():
        existing = {s["url"] for s in asset.get("sources", [])}
        if asset.get("cik"):
            url = _SEC_BY_CIK.format(cik=str(asset["cik"]).zfill(10))
            if url not in existing:
                db.add_source(asset["code"], url, kind="official", source_type="SEC",
                              refresh_interval=60)
                report["sec_added"] += 1
        url = _GOOGLE_NEWS.format(q=_news_query(asset))
        if url not in existing:
            # Переписывать можно только персональную резервную ленту актива. Общие
            # Google News-ленты из методологии (site:lme.com на четыре металла)
            # подписаны на несколько активов — их трогать нельзя: иначе каждый
            # рестарт переписывал бы одну ленту запросом последнего металла,
            # а остальные оставались бы без своей.
            old = next((s for s in asset.get("sources", [])
                        if "news.google.com/rss/search" in s["url"] and _AUTO_FEED_MARK in s["url"]
                        and db.source_subscribers(s["id"]) == [asset["code"]]), None)
            if old:
                # Запрос изменился — переписываем URL существующей ленты актива.
                with db.connect() as conn:
                    taken = conn.execute("SELECT 1 FROM source_defs WHERE url=?",
                                         (url,)).fetchone()
                    if not taken:
                        conn.execute("UPDATE source_defs SET url=?, fail_streak=0 "
                                     "WHERE id=?", (url, old["id"]))
                        report["news_updated"] += 1
            else:
                # Google банит частые запросы: резервные ленты опрашиваем раз в 3-4 часа.
                db.add_source(asset["code"], url, kind="news", source_type="NEWS",
                              refresh_interval=180 + hash(asset["code"]) % 120)
                report["news_added"] += 1
    if any(report.values()):
        log.info("Резервные источники: SEC по CIK +%d, Google News +%d (обновлено %d)",
                 report["sec_added"], report["news_added"], report["news_updated"])
    return report
