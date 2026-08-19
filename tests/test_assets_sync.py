from __future__ import annotations

from pathlib import Path

from app import assets_sync, db

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "underlyings_sources.md"


def test_parse_real_file():
    assets = assets_sync.parse_underlyings_md(MD)
    codes = [a["code"] for a in assets]
    assert len(assets) == 50
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
