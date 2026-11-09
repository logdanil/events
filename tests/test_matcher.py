"""12 обязательных кейсов сопоставления из ТЗ + версии кэша."""
from __future__ import annotations

import pytest

from app import db, matcher, versions


def _profiles():
    assets = [
        {"code": "SPYF", "name": "SPDR S&P 500 ETF Trust (SPY)", "instrument_type": "ETF",
         "underlying_ticker": "SPY",
         "aliases": ["SPY", "SPDR S&P 500 ETF Trust", "SPDR S&P 500 ETF"]},
        {"code": "ALIBABA", "name": "Alibaba Group Holding Ltd – ADR (BABA)", "instrument_type": "ADR",
         "aliases": ["BABA", "Alibaba Group Holding"]},
        {"code": "BAIDU", "name": "Baidu Inc – ADR (BIDU)", "instrument_type": "ADR",
         "aliases": ["BIDU"]},
        {"code": "TENCENT", "name": "Tencent Holdings Ltd (700 HK)", "instrument_type": "SHARE",
         "aliases": ["700 HK", "0700.HK", "Tencent Holdings"]},
        {"code": "GOLD", "name": "LBMA gold", "instrument_type": "ASSET", "aliases": ["GOLD"]},
        {"code": "EM", "name": "iShares MSCI Emerging Markets ETF (EEM)", "instrument_type": "ETF",
         "aliases": ["EEM", "EM"]},
    ]
    return matcher.build_profiles(assets)


def _match(title: str, code: str, **pub_extra) -> matcher.MatchResult:
    profiles = _profiles()
    profile = next(p for p in profiles if p.code == code)
    return matcher.match_one({"title": title, "text": "", **pub_extra}, profile)


# 1. Полное название → high.
def test_full_name_high():
    r = _match("SPDR S&P 500 ETF Trust declares quarterly distribution", "SPYF")
    assert r.confidence == "high"
    assert "exact_full_name" in r.matched_by or "exact_normalized_name" in r.matched_by


# 2. SPY в контексте ETF → high/medium.
def test_spy_ticker_with_etf_context():
    r = _match("SPY ETF announces 5-for-1 unit split", "SPYF")
    assert r.confidence in ("high", "medium")
    assert "ticker_with_context" in r.matched_by


# 3. Слово spy как обычное слово → не match.
def test_spy_as_english_word_no_match():
    r = _match("Government caught a spy near the military base", "SPYF")
    assert r.confidence == "none"
    assert "spy" in r.rejected_terms


# 4. State Street без SPY → low или no match.
def test_state_street_alone_low_or_none():
    r = _match("State Street reports quarterly results", "SPYF")
    assert r.confidence in ("low", "none")


# 5. SEC 8-K посторонней компании → не SPYF.
def test_sec_8k_other_company_not_spyf():
    r = _match("8-K - ACME ROBOTICS INC (0009999999) (Filer)", "SPYF",
               sec_filer="ACME ROBOTICS INC", sec_cik="0009999999", form_type="8-K")
    assert r.score == 0.0 and r.confidence == "none"


def test_sec_matches_by_cik():
    profiles = matcher.build_profiles([
        {"code": "ALIBABA", "name": "Alibaba Group Holding Ltd – ADR (BABA)",
         "instrument_type": "ADR", "cik": "1577552", "aliases": ["BABA"]}])
    pub = {"title": "6-K - Alibaba Group Holding Limited (0001577552) (Filer)",
           "text": "", "sec_filer": "Alibaba Group Holding Limited", "sec_cik": "0001577552"}
    r = matcher.match_one(pub, profiles[0])
    assert r.confidence == "high" and "sec_cik_exact" in r.matched_by


def test_sec_matches_by_exact_filer_name():
    r = _match("6-K - Alibaba Group Holding Ltd (0001577552) (Filer)", "ALIBABA",
               sec_filer="Alibaba Group Holding Ltd", sec_cik="0001577552")
    assert r.confidence == "high" and "sec_filer_exact_name" in r.matched_by


# 6–7. Alibaba/BABA и Baidu/BIDU.
def test_alibaba_and_baidu():
    assert _match("Alibaba Group Holding announces ADR ratio change", "ALIBABA").confidence in ("high", "medium")
    assert _match("BABA shares: tender offer announced", "ALIBABA").confidence in ("high", "medium")
    assert _match("Baidu announces delisting from Nasdaq", "BAIDU").confidence in ("high", "medium")
    assert _match("BIDU ADR conversion notice", "BAIDU").confidence in ("high", "medium")


# 8. Tencent Holdings / 0700.HK.
def test_tencent_by_name_and_local_ticker():
    assert _match("Tencent Holdings announces share buyback", "TENCENT").confidence in ("high", "medium")
    assert _match("Trading suspension notice for 0700.HK shares", "TENCENT").confidence in ("high", "medium")


# 9. Новость о компании из состава S&P 500 → не SPYF.
def test_sp500_constituent_news_not_spyf():
    r = _match("Apple, largest S&P 500 company, announces stock split", "SPYF")
    assert r.confidence in ("low", "none")


# 10. SPY dividend schedule на персональной странице ssga.com → SPYF high.
def test_owned_source_spy_dividend_high():
    r = _match("SPY Dividend Distribution Schedule", "SPYF",
               source_id=7, subscribers=["SPYF"])
    assert r.confidence == "high"
    assert "source_owned_by_asset" in r.matched_by


# 11. Одинаковые короткие тикеры не дают ложных совпадений.
def test_short_ambiguous_tickers_no_false_positives():
    # "EM" и "GOLD" как обычные слова без контекста
    assert _match("Em dash usage in modern typography", "EM").confidence == "none"
    r = _match("Olympic gold medal count rises", "GOLD")
    assert r.confidence == "none"
    # Но с контекстом фонда/биржи тикер срабатывает.
    assert _match("GOLD listing: new futures contract specification", "GOLD").confidence in ("high", "medium")


# 12. Rematch после изменения MATCHER_VERSION: кэш открывает unmatched заново.
def test_cache_reopens_after_matcher_version_bump(tmp_db, monkeypatch):
    uh, th = matcher.url_hash("https://x.com/a"), matcher.title_hash("Some split notice")
    db.remember_article("https://x.com/a", uh, th, None, "s", "skipped:no_asset_match",
                        status="unmatched", title="Some split notice")
    assert db.article_seen(uh, th) is True, "при той же версии matcher публикация закрыта"
    monkeypatch.setattr(versions, "MATCHER_VERSION", versions.MATCHER_VERSION + 1)
    assert db.article_seen(uh, th) is False, "рост MATCHER_VERSION открывает unmatched заново"
    # Финализированные статусы не переоткрываются.
    db.remember_article("https://x.com/b", matcher.url_hash("https://x.com/b"), None,
                        None, "s", "stock_split", status="event_created")
    assert db.article_seen(matcher.url_hash("https://x.com/b")) is True


def test_legacy_rows_reopen(tmp_db):
    """Записи старого формата (без status) с no_asset_match/no_response переоткрываются."""
    with db.connect() as conn:
        conn.execute("INSERT INTO processed_articles(url_hash,url,llm_result) VALUES('h1','u1','skipped:no_asset_match')")
        conn.execute("INSERT INTO processed_articles(url_hash,url,llm_result) VALUES('h2','u2','no_response')")
        conn.execute("INSERT INTO processed_articles(url_hash,url,llm_result) VALUES('h3','u3','stock_split')")
    assert db.article_seen("h1") is False
    assert db.article_seen("h2") is False
    assert db.article_seen("h3") is True, "реально классифицированные не переобрабатываются"


def test_brand_in_asset_own_news_feed_is_medium(tmp_db):
    """Бренд в заголовке собственной ленты Google News/Tavily актива — medium (в Gemini);
    тот же заголовок из чужого/общего источника — low; без текстового совпадения — ничего."""
    db.upsert_asset("TSLA", "Акции Tesla, Inc. (TSLA)", "SHARE", aliases=["Tesla"])
    db.set_asset_field("TSLA", "issuer_name", "Tesla, Inc.")
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    title = "Tesla announces 3-for-1 stock split"

    def best(**src):
        res = matcher.match_publication({"title": title, "text": "", **src}, profiles)
        return res[0] if res else None

    own = best(source_type="NEWS", subscribers=["TSLA"])
    assert own.confidence == "medium" and "asset_news_feed" in own.matched_by
    assert best(source_type="SEARCH", subscribers=["TSLA"]).confidence == "medium"
    assert best(source_type="NEWS", subscribers=[]).confidence == "low", "глобальная лента — без бонуса"
    assert best(source_type="NEWS", subscribers=["TSLA", "A", "B"]).confidence == "low", "общая лента"
    assert best(source_type="NEWS", subscribers=["OTHER"]).confidence == "low", "лента другого актива"
    nothing = matcher.match_publication({"title": "Automaker announces stock split", "text": "",
                                         "source_type": "NEWS", "subscribers": ["TSLA"]}, profiles)
    assert nothing == [], "без имени в тексте собственная лента не даёт кандидата"


def _asian_profiles():
    return matcher.build_profiles([
        {"code": "TOYOTA", "name": "Американские депозитарные расписки на акции Toyota Motor Corporation",
         "instrument_type": "ADR", "underlying_ticker": "TM", "aliases": ["Toyota Motor Corporation"]},
        {"code": "SAMSUNG", "name": "Акции Samsung Electronics", "instrument_type": "SHARE",
         "aliases": ["Samsung Electronics"]},
    ])


@pytest.mark.parametrize("title,code", [
    ("トヨタ自:自己株式の取得に係る事項の一部変更、自己株式の公開買付け並びに自己株式の消却に関するお知らせ",
     "TOYOTA"),
    ("(유가)삼성전자 - 현금ㆍ현물배당결정", "SAMSUNG"),
    ("(유가)삼성전자 - 주요사항보고서(자기주식취득결정)", "SAMSUNG"),
])
def test_asian_regulatory_disclosure_reaches_llm(title, code):
    """TDnet/DART: событие в японском/корейском заголовке проходит фильтр и
    персональная регуляторная лента даёт активу medium без латинского имени."""
    pub = {"title": title, "text": "", "source_type": "EXCHANGE", "subscribers": [code]}
    assert matcher.has_corporate_keywords(pub)
    profiles = _asian_profiles()
    assert matcher.keyword_gate(pub, profiles)
    result = matcher.match_one(pub, next(p for p in profiles if p.code == code))
    assert result.confidence in ("medium", "high")
    assert "source_owned_by_asset" in result.matched_by


def test_routine_dart_ownership_report_is_filtered():
    pub = {"title": "(유가)삼성전자 - 임원ㆍ주요주주특정증권등소유상황보고서", "text": "",
           "source_type": "EXCHANGE", "subscribers": ["SAMSUNG"]}
    assert not matcher.keyword_gate(pub, _asian_profiles())


def test_tdnet_earnings_release_is_filtered():
    pub = {"title": "トヨタ自:2027年3月期 第2四半期（中間期）決算短信〔IFRS〕（連結)", "text": "",
           "source_type": "EXCHANGE", "subscribers": ["TOYOTA"]}
    assert not matcher.keyword_gate(pub, _asian_profiles())


def test_news_aggregator_without_company_name_is_not_owned():
    """Bing/Yahoo (NEWS) не дают владения: новость о другой компании с ключевым
    словом не приписывается Samsung."""
    pub = {"title": "SK Hynix announces share buyback", "text": "",
           "source_type": "NEWS", "subscribers": ["SAMSUNG"]}
    profile = next(p for p in _asian_profiles() if p.code == "SAMSUNG")
    assert matcher.match_one(pub, profile).confidence == "none"


def test_cjk_keywords_do_not_change_latin_matching():
    assert not matcher.has_corporate_keywords({"title": "Toyota unveils new hybrid model", "text": ""})
    assert matcher.has_corporate_keywords({"title": "Toyota announces share buyback", "text": ""})
