"""Дневной Excel-отчёт: содержимое листов, вложение в письмо, документ в Telegram."""
from __future__ import annotations

import dataclasses
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openpyxl import load_workbook

from app import daily_report, db, emailer
from app.config import settings


def _seed(tmp_db):
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust (SPY)", "ETF")
    db.upsert_asset("TSLA", "Tesla Inc", "STOCK")
    db.remember_article("https://x.com/1", "h1", None, "2026-10-29T08:00:00+00:00", "IR SPY",
                        "stock_split", status="event_created", title="SPY announces split",
                        best_asset="SPYF", best_score=0.95, llm_event_type="stock_split",
                        llm_summary="Дробление 1:5")
    db.remember_article("https://x.com/2", "h2", None, None, "Google News",
                        "rejected:confidence 0.40 < 0.72", status="rejected",
                        title="Tesla rumour about dividend", best_asset="TSLA", best_score=0.8,
                        llm_event_type="dividend", llm_summary="Слухи о дивиденде")
    db.remember_article("https://x.com/3", "h3", None, None, "Google News",
                        "skipped:no_asset_match (best 0.45 (TSLA))", status="unmatched",
                        title="Weak Tesla mention", best_asset="TSLA", best_score=0.45)
    # Без кандидата-актива — в отчёт не попадает.
    db.remember_article("https://x.com/4", "h4", None, None, "SEC", "skipped:no_keywords",
                        status="keyword_filtered", title="Routine notice")
    db.save_event({"fingerprint": "fp1", "asset_code": "SPYF", "event_type": "stock_split",
                   "title": "SPY announces split", "summary_ru": "Дробление 1:5",
                   "source_url": "https://x.com/1", "confidence": 0.95, "severity": "CRITICAL",
                   "key_parameters_json": '[{"label": "Коэффициент", "value": "1:5"}]'})


def test_daily_report_rows_only_with_asset(tmp_db):
    _seed(tmp_db)
    rows = db.daily_report_rows(24)
    assert {r["url"] for r in rows} == {"https://x.com/1", "https://x.com/2", "https://x.com/3"}
    spy = next(r for r in rows if r["best_asset"] == "SPYF")
    assert spy["asset_name"].startswith("SPDR") and spy["llm_event_type"] == "stock_split"


def test_workbook_sheets_and_rows(tmp_db):
    _seed(tmp_db)
    content, counts = daily_report.build_workbook(24)
    assert counts == {"publications": 3, "events": 1, "assets": 2, "llm": 2}
    wb = load_workbook(BytesIO(content))
    assert wb.sheetnames == ["Публикации", "События дня", "Сводка по активам"]

    pubs = wb["Публикации"]
    assert [c.value for c in pubs[1]] == daily_report.PUBLICATION_HEADERS
    body = list(pubs.iter_rows(min_row=2, values_only=True))
    assert len(body) == 3
    # Этапы отсортированы от финального к раннему: событие → отклонено → слабый кандидат.
    assert [r[9] for r in body] == ["Событие создано", "Gemini: отклонено",
                                    "Слабый кандидат, в Gemini не передано"]
    first = body[0]
    assert first[1] == "SPYF" and first[6] == "https://x.com/1" and first[10] == 0.95
    assert first[11] == "Дробление акций" and first[12].startswith("Событие:")
    assert first[13] == "Дробление 1:5"
    assert pubs.cell(row=2, column=7).hyperlink.target == "https://x.com/1"
    rejected = body[1]
    assert rejected[11] == "Дивиденд"  # тип от Gemini сохранён и для отклонённых
    assert rejected[12] == "Отклонено: confidence 0.40 < 0.72"

    events = wb["События дня"]
    ev = list(events.iter_rows(min_row=2, values_only=True))
    assert len(ev) == 1 and ev[0][0] == "SPYF" and "Коэффициент: 1:5" in ev[0][6]

    assets = wb["Сводка по активам"]
    rows = list(assets.iter_rows(min_row=2, values_only=True))
    by_code = {r[0]: r for r in rows if r[0] not in (None, "Итого")}
    assert by_code["SPYF"][2:] == (1, 1, 0, 1, 0)
    assert by_code["TSLA"][2:] == (2, 1, 1, 0, 1)
    assert rows[-1][0] == "Итого" and rows[-1][2] == 3


def test_workbook_empty_day(tmp_db):
    content, counts = daily_report.build_workbook(24)
    assert counts["publications"] == 0
    wb = load_workbook(BytesIO(content))
    assert wb["Публикации"].max_row == 1


def test_mime_with_attachment(monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(settings, email_from="bot@x.ru"))
    msg = emailer._build_mime("Тема", "<b>hi</b>", "hi", ["a@b.ru"],
                              [("report.xlsx", b"PK\x03\x04data", emailer.XLSX_MIME)])
    assert msg.get_content_type() == "multipart/mixed"
    parts = msg.get_payload()
    assert parts[0].get_content_type() == "multipart/alternative"
    assert parts[1].get_filename() == "report.xlsx"
    assert parts[1].get_payload(decode=True) == b"PK\x03\x04data"
    plain = emailer._build_mime("Тема", "<b>hi</b>", "hi", ["a@b.ru"])
    assert plain.get_content_type() == "multipart/alternative"


def test_http_payloads_carry_attachment(monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(settings, email_from="bot@x.ru"))
    att = [("r.xlsx", b"abc", emailer.XLSX_MIME)]
    payload = emailer._sendgrid_payload("t", "<b>h</b>", "h", ["a@b.ru"], att)
    assert payload["attachments"][0] == {"content": "YWJj", "filename": "r.xlsx",
                                         "type": emailer.XLSX_MIME, "disposition": "attachment"}
    assert "attachments" not in emailer._sendgrid_payload("t", "<b>h</b>", "h", ["a@b.ru"])


@pytest.mark.asyncio
async def test_send_email_passes_attachments(tmp_db, monkeypatch):
    monkeypatch.setattr(emailer, "settings", dataclasses.replace(
        settings, smtp_host="smtp.test", smtp_user="u@test", smtp_password="secret"))
    db.add_email_recipient("dest@example.com")
    captured = {}

    def fake_send(subject, html_body, plain_body, recipients, attachments=None):
        captured["attachments"] = attachments

    monkeypatch.setattr(emailer, "_send_sync", fake_send)
    att = [("r.xlsx", b"abc", emailer.XLSX_MIME)]
    assert await emailer.send_email("Тема", "<b>текст</b>", attachments=att) is True
    assert captured["attachments"] == att


@pytest.mark.asyncio
async def test_daily_summary_sends_workbook(tmp_db, monkeypatch):
    """Вечерняя сводка: текст + документ в Telegram каждому получателю, письмо с вложением."""
    from app import main as app_main
    _seed(tmp_db)
    db.add_subscriber(222)
    db.add_email_recipient("dest@example.com")
    monkeypatch.setattr(app_main.emailer, "settings", dataclasses.replace(
        settings, smtp_host="smtp.test", smtp_user="u@test", smtp_password="secret"))

    async def fake_summary():
        return "<b>Сводка</b>"

    monkeypatch.setattr(app_main, "build_daily_summary", fake_summary)
    sent_mail = AsyncMock(return_value=True)
    monkeypatch.setattr(app_main.emailer, "send_email", sent_mail)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                          send_document=AsyncMock())
    context = SimpleNamespace(application=SimpleNamespace(bot=bot))
    await app_main.send_daily_summary(context)

    assert bot.send_document.await_count == 2  # администратор 111 + подписчик 222
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["document"].filename == daily_report.report_filename()
    assert "3 публикаций по 2 активам" in kwargs["caption"]
    sent_mail.assert_awaited_once()
    attachments = sent_mail.await_args.kwargs["attachments"]
    assert attachments[0][0] == daily_report.report_filename()
    assert attachments[0][2] == emailer.XLSX_MIME
    load_workbook(BytesIO(attachments[0][1]))  # валидный xlsx


@pytest.mark.asyncio
async def test_daily_summary_survives_workbook_failure(tmp_db, monkeypatch):
    from app import main as app_main

    async def fake_summary():
        return "<b>Сводка</b>"

    monkeypatch.setattr(app_main, "build_daily_summary", fake_summary)
    monkeypatch.setattr(daily_report, "build_workbook",
                        lambda hours: (_ for _ in ()).throw(RuntimeError("boom")))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                          send_document=AsyncMock())
    await app_main.send_daily_summary(SimpleNamespace(application=SimpleNamespace(bot=bot)))
    bot.send_message.assert_awaited()
    bot.send_document.assert_not_awaited()
