from __future__ import annotations

import dataclasses
import os
import re
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


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Изолированная SQLite-база во временной директории."""
    test_settings = dataclasses.replace(settings, db_path=str(tmp_path / "test.db"), admin_telegram_id=111)
    monkeypatch.setattr(db, "settings", test_settings)
    monkeypatch.setattr(bot, "settings", test_settings)
    db.init_db()
    return test_settings


@pytest.fixture
def seeded_db(tmp_db):
    db.seed_from_json(str(ASSETS_JSON), str(PROMPTS_JSON))
    return tmp_db
