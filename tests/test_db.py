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
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
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
