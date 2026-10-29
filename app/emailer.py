"""Дублирование уведомлений на электронную почту.

Транспорты (по приоритету):
- Brevo HTTP API — работает на Railway (SMTP там заблокирован) и в регионах,
  которые не обслуживает Twilio/SendGrid. Нужны BREVO_API_KEY + EMAIL_FROM.
- SendGrid HTTP API — то же, если регион поддерживается Twilio.
- SMTP (Gmail/Яндекс/корпоративный) — для локального запуска и хостингов
  без блокировки портов 465/587.

Функция необязательная: без настроек бот просто не шлёт письма.
На почту уходят события (полный отчёт) и ежедневное саммари с Excel-детализацией дня во вложении —
почасовые отчёты о проверках намеренно не дублируются, чтобы не засорять inbox.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import smtplib
import ssl
import time
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

import httpx

from app import db, formatters
from app.config import settings

log = logging.getLogger(__name__)

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
SMTP2GO_URL = "https://api.smtp2go.com/v3/email/send"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def _gmail_configured() -> bool:
    return bool(settings.gmail_client_id and settings.gmail_client_secret
                and settings.gmail_refresh_token)


def is_configured() -> bool:
    if _gmail_configured():
        return bool(settings.email_from)
    if settings.smtp2go_api_key or settings.brevo_api_key or settings.sendgrid_api_key:
        return bool(settings.email_from)  # HTTP-провайдеры требуют верифицированный from
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


# Вложение: (имя файла, содержимое, MIME-тип).
Attachment = tuple[str, bytes, str]
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _build_mime(subject: str, html_body: str, plain_body: str,
                recipients: list[str], attachments: list[Attachment] | None = None) -> MIMEMultipart:
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(plain_body, "plain", "utf-8"))
    body.attach(MIMEText(html_body, "html", "utf-8"))
    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(body)
        for filename, content, mime in attachments:
            main, _, sub = mime.partition("/")
            part = MIMEBase(main or "application", sub or "octet-stream")
            part.set_payload(content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    else:
        msg = body
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = ", ".join(recipients)
    return msg


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


# Кэш access-токена Gmail: refresh-токен вечный, access живёт ~1 час.
_gmail_token: dict = {"value": "", "expires_at": 0.0}


async def _gmail_access_token(client: httpx.AsyncClient) -> str:
    if _gmail_token["value"] and time.monotonic() < _gmail_token["expires_at"] - 60:
        return _gmail_token["value"]
    response = await client.post(GOOGLE_TOKEN_URL, data={
        "client_id": settings.gmail_client_id,
        "client_secret": settings.gmail_client_secret,
        "refresh_token": settings.gmail_refresh_token,
        "grant_type": "refresh_token",
    })
    if response.status_code >= 400:
        raise RuntimeError(f"Gmail OAuth {response.status_code}: {response.text[:300]}")
    data = response.json()
    _gmail_token["value"] = data["access_token"]
    _gmail_token["expires_at"] = time.monotonic() + int(data.get("expires_in", 3600))
    return _gmail_token["value"]


async def _send_via_gmail(subject: str, html_body: str, plain_body: str,
                          recipients: list[str], attachments: list[Attachment] | None = None) -> None:
    """Отправка через Gmail API от имени самого ящика EMAIL_FROM:
    письма идут через серверы Gmail, SPF/DKIM сходятся — лучшая доставляемость."""
    msg = _build_mime(subject, html_body, plain_body, recipients, attachments)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _gmail_access_token(client)
        response = await client.post(GMAIL_SEND_URL, json={"raw": raw},
                                     headers={"Authorization": f"Bearer {token}"})
        if response.status_code >= 400:
            raise RuntimeError(f"Gmail API {response.status_code}: {response.text[:300]}")


def _send_sync(subject: str, html_body: str, plain_body: str, recipients: list[str],
               attachments: list[Attachment] | None = None) -> None:
    msg = _build_mime(subject, html_body, plain_body, recipients, attachments)
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
                      recipients: list[str], attachments: list[Attachment] | None = None) -> dict:
    payload = {
        "personalizations": [{"to": [{"email": r} for r in recipients]}],
        "from": {"email": settings.email_from, "name": "Corporate Events Monitor"},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain_body},
            {"type": "text/html", "value": html_body},
        ],
    }
    if attachments:
        payload["attachments"] = [{"content": _b64(c), "filename": n, "type": m,
                                   "disposition": "attachment"} for n, c, m in attachments]
    return payload


async def _send_via_smtp2go(subject: str, html_body: str, plain_body: str,
                            recipients: list[str], attachments: list[Attachment] | None = None) -> None:
    payload = {
        "sender": settings.email_from,
        "to": recipients,
        "subject": subject,
        "html_body": html_body,
        "text_body": plain_body,
    }
    if attachments:
        payload["attachments"] = [{"filename": n, "fileblob": _b64(c), "mimetype": m}
                                  for n, c, m in attachments]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(SMTP2GO_URL, json=payload,
                                     headers={"X-Smtp2go-Api-Key": settings.smtp2go_api_key,
                                              "accept": "application/json"})
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        succeeded = data.get("data", {}).get("succeeded", 0)
        if response.status_code >= 400 or not succeeded:
            raise RuntimeError(f"SMTP2GO {response.status_code}: {response.text[:300]}")


async def _send_via_brevo(subject: str, html_body: str, plain_body: str,
                          recipients: list[str], attachments: list[Attachment] | None = None) -> None:
    payload = {
        "sender": {"email": settings.email_from, "name": "Corporate Events Monitor"},
        "to": [{"email": r} for r in recipients],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": plain_body,
    }
    if attachments:
        payload["attachment"] = [{"name": n, "content": _b64(c)} for n, c, _ in attachments]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(BREVO_URL, json=payload,
                                     headers={"api-key": settings.brevo_api_key,
                                              "accept": "application/json"})
        if response.status_code >= 400:
            raise RuntimeError(f"Brevo {response.status_code}: {response.text[:300]}")


async def _send_via_sendgrid(subject: str, html_body: str, plain_body: str,
                             recipients: list[str], attachments: list[Attachment] | None = None) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            SENDGRID_URL,
            json=_sendgrid_payload(subject, html_body, plain_body, recipients, attachments),
            headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"})
        if response.status_code >= 400:
            raise RuntimeError(f"SendGrid {response.status_code}: {response.text[:300]}")


async def send_email(subject: str, telegram_html: str,
                     recipients: list[str] | None = None,
                     attachments: list[Attachment] | None = None) -> bool:
    """Отправляет письмо (HTML + plain-text альтернатива, опционально вложения).
    Best-effort: сбой почты логируется и никогда не мешает Telegram-рассылке."""
    if not is_configured():
        return False
    if recipients is None:
        recipients = [r["email"] for r in db.list_email_recipients()]
    if not recipients:
        return False
    html_body = telegram_html_to_email(telegram_html)
    plain_body = formatters.strip_html(telegram_html)
    try:
        args = (subject, html_body, plain_body, recipients, attachments)
        if _gmail_configured():
            await _send_via_gmail(*args)
        elif settings.smtp2go_api_key:
            await _send_via_smtp2go(*args)
        elif settings.brevo_api_key:
            await _send_via_brevo(*args)
        elif settings.sendgrid_api_key:
            await _send_via_sendgrid(*args)
        else:
            await asyncio.to_thread(_send_sync, *args)
        log.info("Письмо «%s» отправлено %d получателям%s", subject[:60], len(recipients),
                 f", вложений: {len(attachments)}" if attachments else "")
        return True
    except Exception:
        log.exception("Не удалось отправить письмо «%s»", subject[:60])
        return False


def event_subject(event: dict) -> str:
    severity = formatters.classify_severity(event.get("event_type"), event.get("severity"))
    return (f"{formatters.SEVERITY_EMOJI[severity]} {event.get('asset_code', '')} — "
            f"{formatters.event_type_ru(event.get('event_type'))}")
