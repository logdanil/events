"""Дублирование уведомлений на электронную почту.

Два транспорта:
- SendGrid HTTP API (приоритетный) — работает на Railway, где исходящий SMTP
  заблокирован. Нужны SENDGRID_API_KEY и верифицированный отправитель EMAIL_FROM.
- SMTP (Gmail/Яндекс/корпоративный) — для локального запуска и хостингов
  без блокировки портов 465/587.

Функция необязательная: без настроек бот просто не шлёт письма.
На почту уходят события (полный отчёт) и ежедневное саммари —
почасовые отчёты о проверках намеренно не дублируются, чтобы не засорять inbox.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app import db, formatters
from app.config import settings

log = logging.getLogger(__name__)

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


def is_configured() -> bool:
    if settings.sendgrid_api_key:
        return bool(settings.email_from)  # SendGrid требует верифицированный from
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)


def telegram_html_to_email(html_text: str) -> str:
    """Наш Telegram-HTML (<b>, <i>, <code>, <a>) — валидный HTML для письма;
    достаточно перевести переводы строк в <br> и задать базовый стиль."""
    body = html_text.replace("\n", "<br>\n")
    return ('<div style="font-family:-apple-system,\'Segoe UI\',Arial,sans-serif;'
            'font-size:15px;line-height:1.6;color:#1a262d;max-width:640px">'
            f"{body}</div>"
            '<p style="font-family:Arial,sans-serif;font-size:12px;color:#8a969d">'
            "Corporate Events Monitor · автоматическое уведомление</p>")


def _send_sync(subject: str, html_body: str, plain_body: str, recipients: list[str]) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=30) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    else:  # 587 и другие — STARTTLS
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.starttls(context=context)
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)


def _sendgrid_payload(subject: str, html_body: str, plain_body: str,
                      recipients: list[str]) -> dict:
    return {
        "personalizations": [{"to": [{"email": r} for r in recipients]}],
        "from": {"email": settings.email_from, "name": "Corporate Events Monitor"},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain_body},
            {"type": "text/html", "value": html_body},
        ],
    }


async def _send_via_sendgrid(subject: str, html_body: str, plain_body: str,
                             recipients: list[str]) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            SENDGRID_URL,
            json=_sendgrid_payload(subject, html_body, plain_body, recipients),
            headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"})
        if response.status_code >= 400:
            raise RuntimeError(f"SendGrid {response.status_code}: {response.text[:300]}")


async def send_email(subject: str, telegram_html: str,
                     recipients: list[str] | None = None) -> bool:
    """Отправляет письмо (HTML + plain-text альтернатива). Best-effort:
    сбой почты логируется и никогда не мешает Telegram-рассылке."""
    if not is_configured():
        return False
    if recipients is None:
        recipients = [r["email"] for r in db.list_email_recipients()]
    if not recipients:
        return False
    html_body = telegram_html_to_email(telegram_html)
    plain_body = formatters.strip_html(telegram_html)
    try:
        if settings.sendgrid_api_key:
            await _send_via_sendgrid(subject, html_body, plain_body, recipients)
        else:
            await asyncio.to_thread(_send_sync, subject, html_body, plain_body, recipients)
        log.info("Письмо «%s» отправлено %d получателям", subject[:60], len(recipients))
        return True
    except Exception:
        log.exception("Не удалось отправить письмо «%s»", subject[:60])
        return False


def event_subject(event: dict) -> str:
    severity = formatters.classify_severity(event.get("event_type"), event.get("severity"))
    return (f"{formatters.SEVERITY_EMOJI[severity]} {event.get('asset_code', '')} — "
            f"{formatters.event_type_ru(event.get('event_type'))}")
