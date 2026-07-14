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