from __future__ import annotations

import dataclasses
import os
import re
import uuid
from pathlib import Path

import dotenv
import pytest

ROOT = Path(__file__).resolve().parent.parent

# Тесты не читают реальный .env и не видят секретов окружения: настоящие ключи не
# попадут в вывод pytest/CI, а результат не зависит от локальной конфигурации.
dotenv.load_dotenv = lambda *args, **kwargs: False
for _name in re.findall(r'os\.getenv\(\s*"([A-Z0-9_]+)"', (ROOT / "app" / "config.py").read_text("utf-8")):
    os.environ.pop(_name, None)
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN-NOT-REAL",
    "ADMIN_TELEGRAM_ID": "111",
    "GOOGLE_API_KEY": "test-google-key-not-real",
})

from app import bot, db  # noqa: E402
from app.config import settings  # noqa: E402

ASSETS_JSON = ROOT / "data" / "assets.json"
PROMPTS_JSON = ROOT / "data" / "event_prompts.json"


def pytest_addoption(parser):
    parser.addoption("--postgres", action="store_true", help="Run database tests on an isolated PostgreSQL schema")


@pytest.fixture
def postgres_url():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to run PostgreSQL integration tests")
    import psycopg
    from psycopg import sql
    from app import postgres

    schema = "test_events_" + uuid.uuid4().hex
    with psycopg.connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(738541920)")
        conn.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_url = psycopg.conninfo.make_conninfo(
        url, options=f"-c search_path={schema},public -c timezone=UTC")
    try:
        yield isolated_url
    finally:
        postgres.close_pool()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch, request):
    """Изолированная SQLite-база во временной директории."""
    test_settings = dataclasses.replace(settings, db_path=str(tmp_path / "test.db"),
                                       database_url="", admin_telegram_id=111)
    monkeypatch.setattr(db, "settings", test_settings)
    monkeypatch.setattr(bot, "settings", test_settings)
    db.init_db()
    if request.config.getoption("--postgres"):
        url = request.getfixturevalue("postgres_url")
        test_settings = dataclasses.replace(test_settings, database_url=url)
        monkeypatch.setattr(db, "settings", test_settings)
        monkeypatch.setattr(bot, "settings", test_settings)
        db.init_db()
    return test_settings


@pytest.fixture
def seeded_db(tmp_db):
    db.seed_from_json(str(ASSETS_JSON), str(PROMPTS_JSON))
    return tmp_db
