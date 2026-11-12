from __future__ import annotations

from pathlib import Path

from app import assets_sync, db

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "underlyings_sources.md"


def test_parse_real_file():
    assets = assets_sync.parse_underlyings_md(MD)
    codes = [a["code"] for a in assets]
    assert len(assets) == 104  # 50 + 6 криптоиндексов + 21 акция США + 27 акций США (фьючерсы с 10–17.11.2026)
    assert "SPYF" in codes and "IBIT" in codes and "TSM" in codes and "BR, BRM" in codes
    spyf = next(a for a in assets if a["code"] == "SPYF")
    assert spyf["isin"] == "US78462F1030"
    assert spyf["exchange"] == "NYSE"
    assert spyf["sources"] == ["https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-trust-spy"]
    # Дубликаты URL внутри секции схлопываются (NASD в файле дважды).
    nasd = next(a for a in assets if a["code"] == "NASD")
    assert len(nasd["sources"]) == 1
    # Товарные активы без ISIN.
    wti = next(a for a in assets if a["code"] == "WTI")
    assert wti["isin"] is None and wti["sources"]


def test_infer_instrument_type_and_english_alias():
    assert assets_sync.infer_instrument_type(
        "Американские депозитарные расписки на акции SAP SE") == "ADR"
    assert assets_sync.infer_instrument_type("Акции Samsung Electronics") == "SHARE"
    assert assets_sync.infer_instrument_type("iShares Bitcoin Trust ETF") == "ETF"
    assert assets_sync.infer_instrument_type("CME Crude Oil futures") == "ASSET"
    assert assets_sync.english_alias(
        "Американские депозитарные расписки на акции Alibaba Group Holding Limited"
    ) == "Alibaba Group Holding Limited"
    assert assets_sync.english_alias("Акции Samsung Electronics") == "Samsung Electronics"
    assert assets_sync.english_alias("iShares Bitcoin Trust ETF") is None


def test_sync_adds_missing_and_is_idempotent(tmp_db):
    # База знает только SPYF без ISIN и без источников.
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust (SPY)", "ETF", aliases=["SPY"])
    report = assets_sync.sync_from_md(MD)
    assert "TSM" in report["assets_added"] and "IBIT" in report["assets_added"]
    assert "SPYF" not in report["assets_added"]
    assert "SPYF" in report["isin_filled"]
    assert report["sources_added"] > 40

    tsm = db.get_asset("TSM")
    assert tsm["instrument_type"] == "ADR"
    assert tsm["isin"] == "US8740391003"
    assert len(tsm["sources"]) == 2, "страница nasdaq.com без пресс-релизов больше не подключается"
    assert tsm["underlying_ticker"] == "TSM", "тикер — из поля «Тикер» методологии"
    # Английский алиас достался matcher'у.
    assert any("Taiwan Semiconductor" in a for a in tsm["aliases"])
    resolved, _ = db.resolve_asset("Taiwan Semiconductor Manufacturing Company, Limited")
    assert resolved and resolved["code"] == "TSM"

    # KOREA: тикер EWY выведен из nasdaq-URL.
    korea = db.get_asset("KOREA")
    assert any(a == "EWY" for a in korea["aliases"])

    # Повторный запуск ничего не дублирует.
    report2 = assets_sync.sync_from_md(MD)
    assert report2["assets_added"] == [] and report2["sources_added"] == 0
    assert len(db.get_asset("TSM")["sources"]) == 2

def test_backup_sources_for_every_asset(tmp_db):
    db.upsert_asset("TSM", "Американские депозитарные расписки на акции Taiwan Semiconductor", "ADR")
    db.set_asset_field("TSM", "cik", "1046179")
    db.set_asset_field("TSM", "underlying_ticker", "TSM")
    db.upsert_asset("ALUM", "LME Aluminium", "ASSET")
    report = assets_sync.ensure_backup_sources()
    assert report["sec_added"] == 1 and report["news_added"] == 2
    tsm_urls = [s["url"] for s in db.get_asset("TSM")["sources"]]
    assert any("sec.gov" in u and "CIK=0001046179" in u for u in tsm_urls)
    assert any("news.google.com/rss/search" in u and "Taiwan+Semiconductor" in u for u in tsm_urls)
    alum_urls = [s["url"] for s in db.get_asset("ALUM")["sources"]]
    assert not any("sec.gov" in u for u in alum_urls), "без CIK SEC-лента не добавляется"
    assert any("news.google.com" in u and "margin" in u for u in alum_urls), \
        "запрос по товарному активу включает термины ГО"
    # Идемпотентность.
    assert assets_sync.ensure_backup_sources() == {"sec_added": 0, "news_added": 0,
                                                   "news_updated": 0}


def test_backup_news_query_updated_in_place(tmp_db):
    """При смене текста запроса Google News лента актива переписывается на месте,
    а не добавляется второй раз (иначе накапливались бы дубли лент)."""
    db.upsert_asset("ALUM", "LME Aluminium", "ASSET")
    old_url = ("https://news.google.com/rss/search?q=LME+Aluminium+futures+"
               "%28contract+OR+specification%29+when:3d&hl=en-US&gl=US&ceid=US:en")
    db.add_source("ALUM", old_url, kind="news", source_type="NEWS")
    report = assets_sync.ensure_backup_sources()
    assert report["news_updated"] == 1 and report["news_added"] == 0
    urls = [s["url"] for s in db.get_asset("ALUM")["sources"]
            if "news.google.com" in s["url"]]
    assert len(urls) == 1 and "margin" in urls[0], "лента одна и с новым запросом"
    assert assets_sync.ensure_backup_sources()["news_updated"] == 0


def test_per_company_sec_feed_sets_cik_from_url():
    from app import rss_monitor
    feed = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
    <entry><title>6-K  - Report of foreign issuer [Rules 13a-16 and 15d-16]</title>
    <link href="https://www.sec.gov/Archives/edgar/data/1046179/000110465926000001/0001.htm"/>
    <updated>2026-08-18T10:00:00-04:00</updated></entry></feed>"""
    source = {"id": 9, "name": "SEC TSM", "type": "SEC", "subscribers": ["TSM"],
              "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001046179&output=atom"}
    pubs = rss_monitor.parse_feed(feed, source)
    assert pubs[0]["sec_cik"] == "1046179"
    assert pubs[0]["form_type"] == "6-K"


def test_crypto_assets_sync_profile(tmp_db, tmp_path):
    md = tmp_path / "u.md"
    md.write_text("## BTC\n- **Базовый актив:** Индекс МосБиржи Биткоина (MOEXBTC)\n"
                  "- **Биржа первичного листинга:** MOEX\n- **Источники мониторинга:**\n"
                  "  - https://bitcoincore.org/en/rss.xml\n", encoding="utf-8")
    report = assets_sync.sync_from_md(md)
    assert report["assets_added"] == ["BTC"]
    a = db.get_asset("BTC")
    assert a["instrument_type"] == "CRYPTO"
    assert a["issuer_name"] == "Bitcoin" and a["underlying_ticker"] == "BTC"
    assert "Биткоин" in a["aliases"] and "MOEXBTC" in a["aliases"]
    assert "hard+fork" in assets_sync._news_query(a) and "Bitcoin" in assets_sync._news_query(a)
    assert assets_sync.sync_from_md(md)["aliases_added"] == [], "повторная синхронизация идемпотентна"


def test_crypto_matching_and_keywords(tmp_db):
    from app import matcher
    db.upsert_asset("BTC", "Индекс МосБиржи Биткоина (MOEXBTC)", "CRYPTO", aliases=["Биткоин"])
    db.set_asset_field("BTC", "issuer_name", "Bitcoin")
    db.set_asset_field("BTC", "underlying_ticker", "BTC")
    db.upsert_asset("SOL", "Индекс МосБиржи Соланы (MOEXSOL)", "CRYPTO", aliases=["Solana"])
    db.set_asset_field("SOL", "underlying_ticker", "SOL")
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    pub = {"title": "Bitcoin hard fork scheduled: node operators must upgrade before activation", "text": ""}
    assert matcher.has_corporate_keywords(pub)
    res = matcher.match_publication(pub, profiles)
    assert res and res[0].asset_code == "BTC" and res[0].confidence == "high"
    # «sol» без крипто-контекста — не Solana; с контекстом — да.
    assert matcher.match_assets({"title": "Costa del Sol tourism outage report", "text": ""}, profiles) == []
    assert matcher.match_assets({"title": "SOL network outage: Solana validators restart the chain", "text": ""},
                                profiles) == ["SOL"]


def test_json_announcements_parser():
    from app import html_monitor
    src = {"id": 7, "name": "binance.com", "type": "IR", "subscribers": ["BTC"],
           "url": "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1"}
    payload = ('{"data":{"catalogs":[{"articles":[{"id":1,"code":"abc123",'
               '"title":"Binance Will Delist TRX Trading Pairs on 2026-09-01","releaseDate":1756000000000}]}]}}')
    pubs = html_monitor.parse_links(payload, src)
    assert len(pubs) == 1
    assert pubs[0]["url"] == "https://www.binance.com/en/support/announcement/abc123"
    assert pubs[0]["published_dt"].year == 2025 and pubs[0]["subscribers"] == ["BTC"]


def test_us_stock_sync_fields(tmp_db, tmp_path):
    md = tmp_path / "u.md"
    md.write_text("## COIN\n- **Базовый актив:** Акции Coinbase Global, Inc. (COIN)\n"
                  "- **Эмитент:** Coinbase Global, Inc.\n- **Алиасы:** Coinbase\n- **CIK:** `1679788`\n"
                  "- **Биржа первичного листинга:** NASDAQ\n- **Источники мониторинга:**\n"
                  "  - https://www.nasdaq.com/market-activity/stocks/coin/press-releases\n", encoding="utf-8")
    assets_sync.sync_from_md(md)
    a = db.get_asset("COIN")
    assert a["instrument_type"] == "SHARE" and a["cik"] == "1679788"
    assert a["issuer_name"] == "Coinbase Global, Inc." and a["underlying_ticker"] == "COIN"
    assert "Coinbase" in a["aliases"]
    rep = assets_sync.ensure_backup_sources()
    urls = {s["url"] for s in db.get_asset("COIN")["sources"]}
    assert any("sec.gov" in u and "CIK=0001679788" in u for u in urls), "SEC-лента по CIK подключена"


def test_word_tickers_need_explicit_context(tmp_db):
    from app import matcher
    db.upsert_asset("COIN", "Акции Coinbase Global, Inc. (COIN)", "SHARE", aliases=["Coinbase"])
    db.set_asset_field("COIN", "issuer_name", "Coinbase Global, Inc.")
    db.set_asset_field("COIN", "underlying_ticker", "COIN")
    db.upsert_asset("APP", "Акции AppLovin Corporation (APP)", "SHARE", aliases=["AppLovin"])
    db.set_asset_field("APP", "underlying_ticker", "APP")
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    # Слово «coin» в крипто-контексте — не Coinbase; «app» — не AppLovin.
    assert matcher.match_assets({"title": "Bitcoin: the coin rallies as the crypto app market grows", "text": ""}, profiles) == []
    # Явный биржевой контекст — засчитывается.
    assert matcher.match_assets({"title": "Coinbase Global (NASDAQ: COIN) announces stock split", "text": ""}, profiles) == ["COIN"]
    assert matcher.match_assets({"title": "AppLovin announces tender offer for shares", "text": ""}, profiles) == ["APP"]


def test_commodity_profile_sets_issuer_and_aliases(tmp_db, tmp_path):
    """Имя товара совпадает с кодом контракта и как алиас терялось (UNIQUE NOCASE
    против kind='code'); профиль задаёт его как issuer_name — matcher видит «copper»."""
    md = tmp_path / "u.md"
    md.write_text("## COPPER\n- **Базовый актив:** LME Copper\n"
                  "- **Биржа первичного листинга:** London Metal Exchange\n"
                  "- **Алиасы:** Copper, LME Copper\n- **Источники мониторинга:**\n"
                  "  - https://news.google.com/rss/search?q=site:lme.com+when:7d&hl=en-US&gl=US&ceid=US:en\n",
                  encoding="utf-8")
    assets_sync.sync_from_md(md)
    a = db.get_asset("COPPER")
    assert a["instrument_type"] == "ASSET" and a["issuer_name"] == "Copper"
    assert "Copper" not in a["aliases"], "алиас, равный коду, в таблицу не попадает — поэтому нужен issuer"
    assert {"LME Copper", "Медь"} <= set(a["aliases"])
    q = assets_sync._news_query(a)
    assert "copper" in q and "LME" in q and "margin" in q
    assert assets_sync.sync_from_md(md)["aliases_added"] == []


def test_commodity_headlines_match_and_pass_keywords(tmp_db):
    from app import matcher
    for code, name in [("COPPER", "LME Copper"), ("GOLD", "LBMA gold"), ("NICKEL", "LME Nickel"),
                       ("BR, BRM", "ICE Brent Index, ICE Brent futures"), ("WTI", "CME Crude Oil futures")]:
        db.upsert_asset(code, name, "ASSET")
        prof = assets_sync.COMMODITY_PROFILES[code]
        db.set_asset_field(code, "issuer_name", prof["issuer"])
        for alias in prof["aliases"]:
            db.add_alias(code, alias)
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    cases = {
        "LME raises initial margin requirements for copper futures": "COPPER",
        "CME Group lowers gold futures margins by 8%": "GOLD",
        "LME circular: changes to nickel delivery rules": "NICKEL",
        "ICE Brent futures: expiry calendar amendment notice": "BR, BRM",
        "CME raises crude oil futures margin requirements": "WTI",
    }
    for title, expected in cases.items():
        pub = {"title": title, "text": "", "source_type": "NEWS", "subscribers": []}
        assert matcher.has_corporate_keywords(pub), title
        res = matcher.match_publication(pub, profiles)
        assert res and res[0].asset_code == expected and res[0].confidence in ("high", "medium"), title
    # Ценовая новость без признаков события отсеивается до сопоставления.
    assert not matcher.has_corporate_keywords({"title": "Copper price climbs on China demand", "text": ""})


def test_backup_sources_keep_shared_google_news_feed(tmp_db):
    """Общая лента методологии (site:lme.com на четыре металла) — не персональная
    резервная лента: она не переписывается запросом очередного металла, и каждый
    металл получает собственную ленту. Повторные старты ничего не меняют."""
    shared = "https://news.google.com/rss/search?q=site:lme.com+when:7d&hl=en-US&gl=US&ceid=US:en"
    for code, name in [("ALUM", "LME Aluminium"), ("COPPER", "LME Copper"),
                       ("ZINC", "LME Zinc"), ("NICKEL", "LME Nickel")]:
        db.upsert_asset(code, name, "ASSET")
        db.add_source(code, shared, kind="news", source_type="NEWS")
    report = assets_sync.ensure_backup_sources()
    assert report == {"sec_added": 0, "news_added": 4, "news_updated": 0}
    for code in ("ALUM", "COPPER", "ZINC", "NICKEL"):
        urls = [s["url"] for s in db.get_asset(code)["sources"]]
        assert shared in urls, f"{code}: общая лента LME потеряна"
        own = [u for u in urls if u != shared and "news.google.com" in u]
        assert len(own) == 1, code
        assert assets_sync.COMMODITY_PROFILES[code]["issuer"].lower() in own[0].lower(), code
    assert assets_sync.ensure_backup_sources() == {"sec_added": 0, "news_added": 0,
                                                   "news_updated": 0}
    assert db.source_subscribers(db.get_asset("ALUM")["sources"][0]["id"]) == \
        ["ALUM", "COPPER", "NICKEL", "ZINC"]


NEW_US_FUTURES = ("INTC MSTR CRWV CRCL BE ARM MRNA IREN AAOI NOW ADBE RDDT CIEN TEAM GTLB SNOW "
                  "CRDO ALAB SPOT TTWO U DUOL BIIB SHOP ABNB DDOG ON").split()


def test_new_us_futures_in_methodology():
    """27 акций под новые вечные фьючерсы МосБиржи (moex.com/n105658): у каждой —
    эмитент, тикер, CIK (лента SEC) и тип инструмента акции/АДР."""
    parsed = {a["code"]: a for a in assets_sync.parse_underlyings_md(MD)}
    for code in NEW_US_FUTURES:
        item = parsed[code]
        assert item["ticker"] == code and item["cik"] and item["issuer"], code
        assert assets_sync.infer_instrument_type(item["name"]) == ("ADR" if code == "ARM" else "SHARE"), code
        assert not any("nasdaq.com/market-activity" in u for u in item["sources"]), \
            f"{code}: страницы nasdaq.com отдают каркас без пресс-релизов"
    assert all(parsed[c]["sources"] for c in NEW_US_FUTURES if c != "MRNA")


def test_ticker_field_sets_underlying_ticker(tmp_db, tmp_path):
    """Тикер, равный коду актива, как алиас теряется — поле «Тикер» задаёт его напрямую."""
    md = tmp_path / "u.md"
    md.write_text("## NOW\n- **Базовый актив:** Акции ServiceNow, Inc. (NOW)\n"
                  "- **Эмитент:** ServiceNow, Inc.\n- **Тикер:** NOW\n- **Алиасы:** ServiceNow\n"
                  "- **CIK:** `1373715`\n- **Источники мониторинга:**\n"
                  "  - https://newsroom.servicenow.com/rss/PressRelease.aspx?LanguageId=1\n", encoding="utf-8")
    assets_sync.sync_from_md(md)
    a = db.get_asset("NOW")
    assert a["underlying_ticker"] == "NOW" and a["cik"] == "1373715"
    q = assets_sync._news_query(a)
    assert "%22ServiceNow%22" in q and "+OR+NOW+" not in q, "тикер-слово не попадает в запрос Google News"


def test_word_tickers_of_new_us_futures(tmp_db):
    from app import matcher
    for code, name, alias in [("NOW", "ServiceNow, Inc.", "ServiceNow"), ("ON", "ON Semiconductor Corporation", "onsemi"),
                              ("U", "Unity Software Inc.", "Unity Software"), ("SNOW", "Snowflake Inc.", "Snowflake"),
                              ("SHOP", "Shopify Inc.", "Shopify"), ("ARM", "Arm Holdings plc", "Arm Holdings")]:
        db.upsert_asset(code, f"Акции {name} ({code})", "SHARE", aliases=[alias])
        db.set_asset_field(code, "issuer_name", name)
        db.set_asset_field(code, "underlying_ticker", code)
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    for title in ["Shares of tech stocks are now trading lower after split announcement",
                  "Heavy snow forces trading halt at regional exchange",
                  "Shop closures: retailer announces bankruptcy",
                  "Company's arm announces spin-off",
                  "U.S. announces new dividend tax rules",
                  "Investors should be on alert as the merger closes"]:
        assert matcher.match_assets({"title": title, "text": ""}, profiles) == [], title
    assert matcher.match_assets({"title": "ServiceNow (NYSE: NOW) declares special dividend", "text": ""},
                                profiles) == ["NOW"]
    res = matcher.match_publication({"title": "onsemi (Nasdaq: ON) announces share repurchase", "text": ""}, profiles)
    assert res[0].asset_code == "ON" and res[0].confidence == "high", "явный «Nasdaq: ON» — полный вес"
    assert matcher.match_assets({"title": "Unity Software Inc. announces merger", "text": ""}, profiles) == ["U"]


def test_every_asset_has_at_least_two_sources_after_startup(tmp_db):
    """Требование мониторинга: у каждого базового актива — не меньше двух включённых
    источников после стартовой синхронизации (методология + SEC по CIK + Google News
    + проверенные каналы из db.py). У новых акций США — ещё и официальная лента SEC."""
    from app import health
    db.seed_from_json(str(ROOT / "data" / "assets.json"), str(ROOT / "data" / "event_prompts.json"))
    assets_sync.sync_from_md(MD)
    assets_sync.ensure_backup_sources()
    db.init_db()  # подписки из db.py на активы, появившиеся при синхронизации
    health.rehabilitate_sources()
    assets = {a["code"]: a for a in db.list_assets()}
    thin = {c: len(a["sources"]) for c, a in assets.items() if len(a["sources"]) < 2}
    assert not thin, f"меньше двух источников: {thin}"
    for code in NEW_US_FUTURES:
        kinds = [s["kind"] for s in assets[code]["sources"]]
        assert "SEC" in kinds and len(kinds) >= 3, (code, kinds)
    # Moderna без собственной ленты — независимые агрегаторы сверх SEC и Google News.
    assert sum(k == "NEWS" for k in (s["kind"] for s in assets["MRNA"]["sources"])) >= 3
    # onsemi: корень IR-сайта (меню продуктов) заменён на RSS пресс-релизов.
    on_urls = [s["url"] for s in assets["ON"]["sources"]]
    assert "https://investor.onsemi.com/rss/news-releases.xml" in on_urls
    assert "https://investor.onsemi.com/" not in on_urls


def test_onsemi_root_page_migrates_to_rss(tmp_db):
    """Прод: в базе уже подписка на корень IR-сайта onsemi — при старте она переезжает
    на RSS, корень выключается."""
    from app import health
    db.upsert_asset("ON", "Акции ON Semiconductor Corporation (ON)", "SHARE")
    db.add_source("ON", "https://investor.onsemi.com/")
    db.add_source("ON", "https://investor.onsemi.com/rss/news-releases.xml")
    health.rehabilitate_sources()
    urls = [s["url"] for s in db.get_asset("ON")["sources"]]
    assert urls == ["https://investor.onsemi.com/rss/news-releases.xml"]
