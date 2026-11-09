from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from app import db, postgres
from tests.test_db import _sample_event


def _switch(monkeypatch, tmp_db, postgres_url):
    monkeypatch.setattr(db, "settings", dataclasses.replace(tmp_db, database_url=postgres_url))
    db.init_db()


def test_statement_preserves_quoted_text():
    query = "SELECT '?', '100%', ? WHERE key LIKE 'source:%'"
    assert postgres.statement(query) == "SELECT '?', '100%%', %s WHERE key LIKE 'source:%%'"


def test_row_supports_named_and_positional_access():
    row = postgres.Row(["id", "name"], [42, "x"])
    assert row[0] == row["id"] == 42
    assert dict(row) == {"id": 42, "name": "x"}


def test_migration_preserves_memory_ids_and_sequences(tmp_db, postgres_url, monkeypatch):
    if tmp_db.database_url:
        pytest.skip("Migration test needs a SQLite source")
    db.upsert_asset("TSM", "Taiwan Semiconductor", "ADR", ["TSMC"])
    db.add_source("TSM", "https://example.com/feed")
    source = next(s for s in db.list_source_defs() if s["url"] == "https://example.com/feed")
    db.add_subscriber(9876543210123, "Иван")
    db.add_email_recipient("Test@Example.com", "Получатель")
    db.add_matcher_rule("TSM", "negative", "unrelated")
    event_id = db.save_event(_sample_event())
    db.mark_sent("fp-1")
    db.remember_article("https://example.com/a", "hash", "title-hash", "2026-11-09",
                        "Example", '{"result":"cached"}', title="Сплит", text="Saved text",
                        best_asset="TSM", best_score=0.123456789123, pending_llm=True,
                        source_id=source["id"], llm_summary="Сохранено")
    db.set_title_ru({"hash": "Перевод"})
    db.save_unmatched("hash", "TSM", 0.123456789123, '[{"code":"TSM"}]', "weak")
    db.record_llm_usage("gemini", "test", 1, 123, 45, 0.123456789123, 2)
    run_id = db.start_scan_run()
    db.finish_scan_run(run_id, "ok", 1, 1)
    db.set_setting("reports:9876543210123", "off")
    with db.connect() as conn:
        conn.execute("UPDATE sqlite_sequence SET seq=500 WHERE name='scan_runs'")
        conn.execute("UPDATE events SET rowid=700 WHERE rowid=?", (event_id,))
    before = {
        "assets": db.list_assets(), "sources": db.list_source_defs(),
        "article": db.get_article("hash"), "queue": db.pending_matched_publications(),
        "recipients": db.list_email_recipients(), "subscribers": db.list_subscribers(),
        "usage": db.cost_stats(), "runs": db.list_scan_runs(),
        "unmatched": db.list_unmatched(), "rules": db.list_matcher_rules(),
        "event": db.get_event(700),
    }
    _switch(monkeypatch, tmp_db, postgres_url)
    after = {
        "assets": db.list_assets(), "sources": db.list_source_defs(),
        "article": db.get_article("hash"), "queue": db.pending_matched_publications(),
        "recipients": db.list_email_recipients(), "subscribers": db.list_subscribers(),
        "usage": db.cost_stats(), "runs": db.list_scan_runs(),
        "unmatched": db.list_unmatched(), "rules": db.list_matcher_rules(),
        "event": db.get_event(700),
    }
    assert before == after
    assert db.unsent_events() == []
    assert db.save_event(_sample_event()) is None
    assert db.get_setting("reports:9876543210123") == "off"
    assert db.start_scan_run() == 501
    new_event_id = db.save_event(_sample_event("new-event"))
    assert new_event_id > 700
    with db.connect() as conn:
        migration = conn.execute("SELECT * FROM sqlite_migrations").fetchone()
        manifest = json.loads(migration["manifest_json"])
        assert len(manifest) == 16
        assert manifest["processed_articles"]["rows"] == 1
        assert Path(migration["backup_path"]).exists()
    postgres.close_pool()
    db.init_db()
    assert db.get_event(700) and db.get_event(new_event_id)
    assert db.start_scan_run() == 502


def test_migration_rejects_nonempty_target(tmp_db, postgres_url, monkeypatch):
    if tmp_db.database_url:
        pytest.skip("Migration test needs a SQLite source")
    import psycopg

    with psycopg.connect(postgres_url) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO settings VALUES ('existing', 'keep')")
    with pytest.raises(RuntimeError, match="not empty"):
        _switch(monkeypatch, tmp_db, postgres_url)
    with psycopg.connect(postgres_url) as conn:
        assert conn.execute("SELECT value FROM settings").fetchone()[0] == "keep"
        assert conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() "
            "AND table_name='assets'").fetchone() is None


def test_migration_rejects_missing_volume(tmp_db, postgres_url, monkeypatch):
    if tmp_db.database_url:
        pytest.skip("Migration test needs a SQLite source")
    missing = dataclasses.replace(tmp_db, db_path=str(Path(tmp_db.db_path).with_name("missing.db")))
    with pytest.raises(RuntimeError, match="does not exist"):
        _switch(monkeypatch, missing, postgres_url)
    assert not Path(missing.db_path).exists()


def test_migration_rejects_unknown_columns(tmp_db, postgres_url, monkeypatch):
    if tmp_db.database_url:
        pytest.skip("Migration test needs a SQLite source")
    with db.connect() as conn:
        conn.execute("ALTER TABLE assets ADD COLUMN important_memory TEXT")
    with pytest.raises(RuntimeError, match="Unmapped columns"):
        _switch(monkeypatch, tmp_db, postgres_url)


def test_postgres_upsert_retains_cached_fields(tmp_db, postgres_url, monkeypatch):
    if not tmp_db.database_url:
        _switch(monkeypatch, tmp_db, postgres_url)
    db.remember_article("https://example.com/a", "hash", None, None, "Example", None,
                        title="Original", text="Kept", best_score=0.9)
    db.remember_article("https://example.com/a", "hash", None, None, "Example", "cached",
                        status="rejected")
    row = db.get_article("hash")
    assert row["title"] == "Original" and row["text"] == "Kept" and row["best_score"] == 0.9
    assert row["llm_result"] == "cached" and row["status"] == "rejected"


def test_postgres_case_insensitive_alias_and_rollback(tmp_db, postgres_url, monkeypatch):
    if not tmp_db.database_url:
        _switch(monkeypatch, tmp_db, postgres_url)
    db.upsert_asset("TSM", "Taiwan Semiconductor", "ADR", ["TSMC"])
    assert db.resolve_asset("tsmc")[0]["code"] == "TSM"
    assert not db.add_alias("TSM", "tsmc")
    with pytest.raises(RuntimeError, match="abort"):
        with db.connect() as conn:
            conn.execute("INSERT INTO settings VALUES ('must_rollback', 'x')")
            raise RuntimeError("abort")
    assert db.get_setting("must_rollback") == ""
