from __future__ import annotations

from app import db
from tests.conftest import ASSETS_JSON, PROMPTS_JSON


def _sample_event(fingerprint: str = "fp-1") -> dict:
    return {
        "fingerprint": fingerprint,
        "asset_code": "TSM",
        "event_type": "Делистинг",
        "event_date": "2026-07-01",
        "title": "Test event",
        "summary_ru": "Описание",
        "impact_ru": "Влияние",
        "source_url": "https://example.com/notice",
        "source_name": "Example",
        "confidence": 0.9,
    }


def test_init_db_creates_tables(tmp_db):
    with db.connect() as conn:
        query = ("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
                 if tmp_db.database_url else "SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in conn.execute(query)}
    assert {"assets", "sources", "event_prompts", "events", "settings"} <= tables


def test_seed_loads_assets_and_prompts(seeded_db):
    assets = db.list_assets()
    assert len(assets) == 36
    with db.connect() as conn:
        prompt_count = conn.execute("SELECT COUNT(*) FROM event_prompts").fetchone()[0]
    assert prompt_count == 234
    # В исходных данных у STOX нет официальных источников — известное свойство методологии.
    assert [a["code"] for a in assets if not a["sources"]] == ["STOX"]


def test_seed_is_idempotent(seeded_db):
    db.seed_from_json(str(ASSETS_JSON), str(PROMPTS_JSON))
    assert len(db.list_assets()) == 36
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_prompts").fetchone()[0] == 234


def test_event_deduplication(tmp_db):
    event = _sample_event()
    first_id = db.save_event(event)
    assert isinstance(first_id, int)
    assert db.event_exists(event["fingerprint"]) is True
    assert db.save_event(event) is None, "повторное сохранение того же события должно отклоняться"
    second_id = db.save_event(_sample_event("fp-2"))
    assert isinstance(second_id, int) and second_id != first_id


def test_unsent_events_and_mark_sent(tmp_db):
    event = _sample_event()
    db.save_event(event)
    assert [e["fingerprint"] for e in db.unsent_events()] == [event["fingerprint"]]
    db.mark_sent(event["fingerprint"])
    assert db.unsent_events() == []


def test_settings_roundtrip(tmp_db):
    db.set_setting("interval_minutes", "90")
    assert db.get_setting("interval_minutes") == "90"
    assert db.get_setting("missing", "default") == "default"


def test_scan_runs_and_db_stats(tmp_db):
    run_id = db.start_scan_run()
    db.finish_scan_run(run_id, "ok", assets_scanned=3, new_events=1)
    stats = db.db_stats()
    assert stats["exists"] is True
    assert stats["size_bytes"] > 0
    assert stats["scan_runs"] == 1
    assert stats["last_run"]["status"] == "ok"
    assert stats["last_run"]["new_events"] == 1


def test_toyota_official_backup_preserves_global_source(tmp_db):
    db.upsert_asset("TOYOTA", "Toyota Motor Corporation", "ADR")
    db.add_source("TOYOTA", "https://global.toyota/en/newsroom/")
    db.init_db()
    sources = db.get_asset("TOYOTA")["sources"]
    assert {"https://global.toyota/en/newsroom/", "https://pressroom.toyota.com/feed/"} <= {
        s["url"] for s in sources}
    backup = next(s for s in db.list_source_defs() if s["url"] == "https://pressroom.toyota.com/feed/")
    assert backup["parser"] == "rss" and backup["subscribers"] == ["TOYOTA"]
    assert "резерв" in backup["name"]


def test_samsung_toyota_supplementary_sources(tmp_db):
    """Дополнительные каналы для заблокированных Samsung/Toyota подключаются
    рядом с исходными (не вместо) и с корректными типами владения."""
    db.upsert_asset("SAMSUNG", "Акции Samsung Electronics", "SHARE")
    db.upsert_asset("TOYOTA", "Toyota Motor Corporation", "ADR")
    db.add_source("SAMSUNG", "https://news.samsung.com/global/category/press-resources/press-release")
    db.add_source("TOYOTA", "https://global.toyota/en/newsroom/")
    db.init_db()
    by_url = {s["url"]: s for s in db.list_source_defs()}
    expected = {
        "https://dart.fss.or.kr/api/companyRSS.xml?crpCd=00126380": ("SAMSUNG", "EXCHANGE"),
        "https://webapi.yanoshin.jp/webapi/tdnet/list/7203.rss": ("TOYOTA", "EXCHANGE"),
        "https://news.google.com/rss/search?q=site:news.samsung.com+when:7d&hl=en-US&gl=US&ceid=US:en":
            ("SAMSUNG", "IR"),
        "https://news.google.com/rss/search?q=site:global.toyota+when:7d&hl=en-US&gl=US&ceid=US:en":
            ("TOYOTA", "IR"),
        "https://www.bing.com/news/search?q=%22Samsung+Electronics%22&format=rss": ("SAMSUNG", "NEWS"),
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=005930.KS&region=US&lang=en-US":
            ("SAMSUNG", "NEWS"),
        "https://www.bing.com/news/search?q=%22Toyota+Motor%22&format=rss": ("TOYOTA", "NEWS"),
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=TM&region=US&lang=en-US": ("TOYOTA", "NEWS"),
    }
    for url, (code, source_type) in expected.items():
        assert by_url[url]["subscribers"] == [code] and by_url[url]["type"] == source_type
        assert by_url[url]["parser"] == "rss" and by_url[url]["enabled"] == 1
    assert "https://news.samsung.com/global/category/press-resources/press-release" in by_url
    assert "https://global.toyota/en/newsroom/" in by_url


def test_toyota_cik_enables_sec_feed(tmp_db):
    from app import assets_sync
    from tests.conftest import ROOT
    assets_sync.sync_from_md(ROOT / "underlyings_sources.md")
    assets_sync.ensure_backup_sources()
    assert db.get_asset("TOYOTA")["cik"] == "1094517"
    sec = [s for s in db.list_source_defs() if "CIK=0001094517" in s["url"]]
    assert len(sec) == 1 and sec[0]["type"] == "SEC" and sec[0]["subscribers"] == ["TOYOTA"]
