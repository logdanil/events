"""Email-дублирование: конвертация, адресаты, команды, интеграция с рассылкой."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot, db, emailer, formatters
from app.config import settings
from tests.test_bot import make_context, make_update


def _text(update) -> str:
    return "\n".join(c.args[0] for c in update.effective_message.reply_text.await_args_list)


def test_not_configured_without_smtp(monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(settings, smtp_host="", smtp_user=""))
    assert emailer.is_configured() is False


@pytest.mark.asyncio
async def test_send_email_noop_without_config(tmp_db, monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(settings, smtp_host=""))
    assert await emailer.send_email("тема", "<b>текст</b>") is False


def test_telegram_html_to_email():
    result = emailer.telegram_html_to_email("🚨 <b>Дробление акций</b>\n<code>SPYF</code>")
    assert "🚨 <b>Дробление акций</b><br>" in result
    assert "<code>SPYF</code>" in result
    assert "font-family" in result


def test_event_subject():
    subject = emailer.event_subject({"asset_code": "SPYF", "event_type": "stock_split",
                                     "severity": "CRITICAL"})
    assert subject == "🔴 SPYF — Дробление акций"


@pytest.mark.asyncio
async def test_send_email_calls_smtp(tmp_db, monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, smtp_host="smtp.test", smtp_user="u@test", smtp_password="secret"))
    db.add_email_recipient("dest@example.com")
    captured = {}

    def fake_send(subject, html_body, plain_body, recipients):
        captured.update(subject=subject, html=html_body, plain=plain_body, recipients=recipients)

    monkeypatch.setattr(emailer, "_send_sync", fake_send)
    ok = await emailer.send_email("Тема", "<b>Событие</b>\nстрока")
    assert ok is True
    assert captured["recipients"] == ["dest@example.com"]
    assert "<b>Событие</b><br>" in captured["html"]
    assert captured["plain"] == "Событие\nстрока"


def test_sendgrid_configured_requires_from(monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, sendgrid_api_key="SG.x", email_from="", smtp_host=""))
    assert emailer.is_configured() is False, "SendGrid без верифицированного from не считается настроенным"
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, sendgrid_api_key="SG.x", email_from="alerts@corp.ru"))
    assert emailer.is_configured() is True


def test_sendgrid_payload(monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, sendgrid_api_key="SG.x", email_from="alerts@corp.ru"))
    payload = emailer._sendgrid_payload("Тема", "<b>html</b>", "plain", ["a@b.ru", "c@d.ru"])
    assert payload["from"]["email"] == "alerts@corp.ru"
    assert payload["subject"] == "Тема"
    assert [t["email"] for t in payload["personalizations"][0]["to"]] == ["a@b.ru", "c@d.ru"]
    assert payload["content"][0] == {"type": "text/plain", "value": "plain"}
    assert payload["content"][1]["type"] == "text/html"


@pytest.mark.asyncio
async def test_send_email_prefers_sendgrid_over_smtp(tmp_db, monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, sendgrid_api_key="SG.x", email_from="alerts@corp.ru",
        smtp_host="smtp.test", smtp_user="u", smtp_password="p"))
    db.add_email_recipient("dest@example.com")
    called = {}

    async def fake_sendgrid(subject, html_body, plain_body, recipients):
        called["via"] = "sendgrid"
        called["recipients"] = recipients

    def fake_smtp(*args):
        called["via"] = "smtp"

    monkeypatch.setattr(emailer, "_send_via_sendgrid", fake_sendgrid)
    monkeypatch.setattr(emailer, "_send_sync", fake_smtp)
    assert await emailer.send_email("Тема", "<b>текст</b>") is True
    assert called["via"] == "sendgrid", "при заданном SENDGRID_API_KEY SMTP не используется"
    assert called["recipients"] == ["dest@example.com"]


@pytest.mark.asyncio
async def test_gmail_has_top_priority(tmp_db, monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, gmail_client_id="cid", gmail_client_secret="cs",
        gmail_refresh_token="rt", smtp2go_api_key="s2g-x", email_from="me@gmail.com"))
    db.add_email_recipient("dest@example.com")
    called = {}

    async def fake_gmail(subject, html_body, plain_body, recipients):
        called["via"] = "gmail"
        called["recipients"] = recipients

    async def fake_other(*args):
        called["via"] = "other"

    monkeypatch.setattr(emailer, "_send_via_gmail", fake_gmail)
    monkeypatch.setattr(emailer, "_send_via_smtp2go", fake_other)
    assert emailer.is_configured() is True
    assert await emailer.send_email("Тема", "<b>текст</b>") is True
    assert called["via"] == "gmail", "Gmail API приоритетнее остальных транспортов"
    assert called["recipients"] == ["dest@example.com"]


def test_gmail_mime_roundtrip(monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, email_from="me@gmail.com"))
    msg = emailer._build_mime("Тема письма", "<b>html</b>", "plain", ["a@b.ru"])
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "a@b.ru"
    raw = msg.as_bytes()
    assert b"html" in raw and b"plain" in raw


@pytest.mark.asyncio
async def test_smtp2go_has_top_priority(tmp_db, monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, smtp2go_api_key="s2g-x", brevo_api_key="brevo-x",
        sendgrid_api_key="SG.x", email_from="alerts@corp.ru"))
    db.add_email_recipient("dest@example.com")
    called = {}

    async def fake_smtp2go(subject, html_body, plain_body, recipients):
        called["via"] = "smtp2go"
        called["recipients"] = recipients

    async def fake_other(*args):
        called["via"] = "other"

    monkeypatch.setattr(emailer, "_send_via_smtp2go", fake_smtp2go)
    monkeypatch.setattr(emailer, "_send_via_brevo", fake_other)
    monkeypatch.setattr(emailer, "_send_via_sendgrid", fake_other)
    assert await emailer.send_email("Тема", "<b>текст</b>") is True
    assert called["via"] == "smtp2go"
    assert called["recipients"] == ["dest@example.com"]


@pytest.mark.asyncio
async def test_brevo_has_priority_and_payload(tmp_db, monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, brevo_api_key="brevo-x", sendgrid_api_key="SG.x",
        email_from="alerts@corp.ru"))
    db.add_email_recipient("dest@example.com")
    called = {}

    async def fake_brevo(subject, html_body, plain_body, recipients):
        called["via"] = "brevo"
        called["recipients"] = recipients

    async def fake_sendgrid(*args):
        called["via"] = "sendgrid"

    monkeypatch.setattr(emailer, "_send_via_brevo", fake_brevo)
    monkeypatch.setattr(emailer, "_send_via_sendgrid", fake_sendgrid)
    assert await emailer.send_email("Тема", "<b>текст</b>") is True
    assert called["via"] == "brevo", "Brevo приоритетнее SendGrid"
    assert called["recipients"] == ["dest@example.com"]


def test_email_recipients_crud(tmp_db):
    assert db.add_email_recipient("A@Example.com", "Иван") is True
    assert db.add_email_recipient("a@example.com") is False, "регистр не создаёт дубль"
    assert [r["email"] for r in db.list_email_recipients()] == ["a@example.com"]
    assert db.remove_email_recipient("A@EXAMPLE.COM") is True
    assert db.list_email_recipients() == []


@pytest.mark.asyncio
async def test_addemail_command_validation(tmp_db):
    update = make_update()
    await bot.add_email(update, make_context(["не-адрес"]))
    assert "Формат:" in _text(update)
    update2 = make_update()
    await bot.add_email(update2, make_context(["user@corp.ru", "Иван"]))
    assert "добавлен" in _text(update2)
    assert db.list_email_recipients()[0]["email"] == "user@corp.ru"
    # Почта не настроена — команда честно предупреждает.
    assert "Почта не настроена" in _text(update2)


@pytest.mark.asyncio
async def test_addemail_batch(tmp_db):
    update = make_update()
    await bot.add_email(update, make_context(["a@x.ru", "b@y.ru,", "c@z.ru", "мусор"]))
    text = _text(update)
    assert "Добавлено адресов: 3" in text
    assert {r["email"] for r in db.list_email_recipients()} == {"a@x.ru", "b@y.ru", "c@z.ru"}
    assert "Не похоже на адреса" in text and "мусор" in text
    # Повторная пачка: дубликаты не создаются.
    update2 = make_update()
    await bot.add_email(update2, make_context(["a@x.ru", "new@corp.ru"]))
    text2 = _text(update2)
    assert "Добавлено адресов: 1" in text2 and "Уже были в списке: a@x.ru" in text2
    assert len(db.list_email_recipients()) == 4


@pytest.mark.asyncio
async def test_emails_denied_for_non_admin(tmp_db):
    db.add_subscriber(555)
    for handler in (bot.add_email, bot.remove_email, bot.emails, bot.test_email_cmd):
        update = make_update(user_id=555)
        await handler(update, make_context(["x@y.com"]))
        assert "Нет доступа" in _text(update), handler.__name__


@pytest.mark.asyncio
async def test_send_events_emails_full_report(tmp_db, monkeypatch):
    """При рассылке события письмо уходит с полным отчётом."""
    from app import main as app_main
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust (SPY)", "ETF")
    event_id = db.save_event({"fingerprint": "fp-email", "asset_code": "SPYF",
                              "event_type": "stock_split", "title": "Split announced",
                              "summary_ru": "Дробление 1:5", "source_url": "https://x.com/1",
                              "confidence": 0.9, "severity": "CRITICAL"})
    sent_mail = AsyncMock(return_value=True)
    monkeypatch.setattr(app_main.emailer, "send_email", sent_mail)
    fake_app = SimpleNamespace(bot=SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))))
    event = db.get_event(event_id)
    await app_main.send_events(fake_app, [event])
    sent_mail.assert_awaited_once()
    subject, body = sent_mail.await_args.args
    assert subject == "🔴 SPYF — Дробление акций"
    assert "ДРОБЛЕНИЕ АКЦИЙ" in body and "SPYF" in body
