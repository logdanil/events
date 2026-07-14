from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app import bot, db
from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
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
