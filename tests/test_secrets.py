"""Секреты не должны утекать в repr, логи и сообщения."""
from __future__ import annotations

import dataclasses
import logging

from app import config
from app.config import SECRET_FIELDS, Settings, redact, settings


def _with_secrets() -> Settings:
    return dataclasses.replace(settings, **{name: f"SECRET-{name}-value" for name in SECRET_FIELDS})


def test_all_credentials_are_secret_fields():
    assert set(SECRET_FIELDS) == {
        "telegram_bot_token", "google_api_key", "tavily_api_key", "gmail_client_secret",
        "gmail_refresh_token", "smtp2go_api_key", "brevo_api_key", "sendgrid_api_key", "smtp_password"}


def test_repr_hides_secrets():
    text = repr(_with_secrets())
    assert "SECRET-" not in text
    assert "db_path=" in text, "несекретные поля по-прежнему видны для диагностики"


def test_redact_masks_every_secret():
    s = _with_secrets()
    text = " | ".join(getattr(s, name) for name in SECRET_FIELDS)
    masked = redact(f"url=https://api.telegram.org/bot{s.telegram_bot_token}/send {text}", s)
    assert "SECRET-" not in masked and masked.count("***") == len(SECRET_FIELDS) + 1


def test_log_formatter_and_health_alerts_mask_secrets(monkeypatch, tmp_db):
    from app import health, main
    s = _with_secrets()
    monkeypatch.setattr(config, "settings", s)
    record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                               "ошибка запроса %s", (f"key={s.google_api_key}",), None)
    assert "SECRET-" not in main._RedactingFormatter("%(message)s").format(record)
    assert "SECRET-" not in main._redact(f"Bearer {s.sendgrid_api_key}")
    alert = health.report("test", "leak", f"SMTP login failed: {s.smtp_password}")
    assert alert and "SECRET-" not in alert["message"]


def test_tests_do_not_read_real_env():
    assert settings.telegram_bot_token == "123456:TEST-TOKEN-NOT-REAL"
    assert not settings.gmail_refresh_token and not settings.tavily_api_key
