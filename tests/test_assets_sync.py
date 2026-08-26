from __future__ import annotations

from pathlib import Path

from app import assets_sync, db

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "underlyings_sources.md"


def test_parse_real_file():
    assets = assets_sync.parse_underlyings_md(MD)
    codes = [a["code"] for a in assets]
    assert len(assets) == 56  # 50 + 6 криптоиндексов МосБиржи
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
    assert len(tsm["sources"]) == 3
    # Английский алиас и тикер из URL достались matcher'у.
    assert any("Taiwan Semiconductor" in a for a in tsm["aliases"])
    resolved, _ = db.resolve_asset("Taiwan Semiconductor Manufacturing Company, Limited")
    assert resolved and resolved["code"] == "TSM"

    # KOREA: тикер EWY выведен из nasdaq-URL.
    korea = db.get_asset("KOREA")
    assert any(a == "EWY" for a in korea["aliases"])

    # Повторный запуск ничего не дублирует.
    report2 = assets_sync.sync_from_md(MD)
    assert report2["assets_added"] == [] and report2["sources_added"] == 0
    assert len(db.get_asset("TSM")["sources"]) == 3

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
    assert any("news.google.com" in u for u in alum_urls)
    # Идемпотентность.
    assert assets_sync.ensure_backup_sources() == {"sec_added": 0, "news_added": 0}


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
