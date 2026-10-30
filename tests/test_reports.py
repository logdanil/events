"""Отчёты о проверках (/reports), история (/scans), просмотр публикаций (/articles)."""
from __future__ import annotations

import json

import pytest

from app import bot, db, formatters, matcher
from tests.test_bot import make_context, make_update


def _text(update) -> str:
    return "\n".join(c.args[0] for c in update.effective_message.reply_text.await_args_list)


@pytest.mark.asyncio
async def test_reports_toggle_is_personal(tmp_db):
    """/reports on|off меняет только настройку того, кто её вызвал."""
    db.add_subscriber(555)
    update = make_update()
    await bot.reports_cmd(update, make_context([]))
    assert "включены" in _text(update), "отчёты о проверках включены по умолчанию"
    assert "/reports all" in _text(update), "администратору видна настройка по умолчанию"

    update2 = make_update(user_id=555)
    await bot.reports_cmd(update2, make_context(["выкл"]))
    assert "выключены" in _text(update2)
    assert bot.scan_reports_enabled(555) is False
    assert bot.scan_reports_enabled(bot.settings.admin_telegram_id) is True, "админа не задело"
    assert db.get_setting(bot.SCAN_REPORTS_KEY, "1") == "1", "общая настройка не изменилась"

    update3 = make_update(user_id=555)
    await bot.reports_cmd(update3, make_context([]))
    assert "выключены (ваш выбор)" in _text(update3)
    assert "/reports all" not in _text(update3)

    update4 = make_update(user_id=555)
    await bot.reports_cmd(update4, make_context(["on"]))
    assert bot.scan_reports_enabled(555) is True


@pytest.mark.asyncio
async def test_reports_default_for_all(tmp_db):
    """/reports all off — по умолчанию выкл. для всех, кто не выбрал сам; личный выбор сильнее."""
    db.add_subscriber(555)
    db.add_subscriber(777)
    await bot.reports_cmd(make_update(user_id=777), make_context(["on"]))
    update = make_update()
    await bot.reports_cmd(update, make_context(["all", "off"]))
    assert db.get_setting(bot.SCAN_REPORTS_KEY) == "0"
    assert bot.scan_reports_enabled(555) is False
    assert bot.scan_reports_enabled(777) is True
    admin = bot.settings.admin_telegram_id
    assert bot.scan_report_recipients(has_events=False) == [777]
    assert bot.scan_report_recipients(has_events=True) == [admin, 555, 777], \
        "проверка с новыми событиями приходит всем"
    # Подписчик не может менять настройку по умолчанию.
    sub = make_update(user_id=555)
    await bot.reports_cmd(sub, make_context(["all", "on"]))
    assert "Нет доступа" in _text(sub) and db.get_setting(bot.SCAN_REPORTS_KEY) == "0"


@pytest.mark.asyncio
async def test_scans_history(tmp_db):
    run_id = db.start_scan_run()
    db.finish_scan_run(run_id, "ok", assets_scanned=50, new_events=2, errors=0)
    with db.connect() as conn:
        conn.execute("UPDATE scan_runs SET stats_json=? WHERE id=?",
                     (json.dumps({"publications": 1098, "cache_hits": 900,
                                  "after_filter": 5, "saved": 2, "cost_usd": 0.0042}), run_id))
    update = make_update()
    await bot.scans_history(update, make_context([]))
    text = _text(update)
    assert "История проверок" in text
    assert "публикаций 1098" in text and "кэш 900" in text
    assert "в LLM 5" in text and "событий 2" in text and "$0.0042" in text


@pytest.mark.asyncio
async def test_articles_listing(tmp_db):
    db.remember_article("https://x.com/1", matcher.url_hash("https://x.com/1"), None, None,
                        "SEC EDGAR", "skipped:no_keywords", status="keyword_filtered",
                        title="Routine 10-Q filing")
    db.remember_article("https://x.com/2", matcher.url_hash("https://x.com/2"), None, None,
                        "Tavily", "stock_split", status="event_created",
                        title="SPY announces split", best_asset="SPYF", best_score=0.9)
    update = make_update()
    await bot.articles(update, make_context([]))
    text = _text(update)
    assert "Routine 10-Q filing" in text and "без признаков события" in text
    assert "SPY announces split" in text and "создано событие" in text
    assert "кандидат: SPYF" in text
    assert "Новых за 24 ч по источникам:" in text
    # Фильтр по источнику: /articles SEC показывает только SEC-записи.
    update2 = make_update()
    await bot.articles(update2, make_context(["SEC"]))
    text2 = _text(update2)
    assert "Routine 10-Q filing" in text2 and "SPY announces split" not in text2


def test_format_scan_history_empty_and_error():
    assert formatters.format_scan_history([]) == "Проверок ещё не было."
    runs = [{"id": 7, "started_at": "2026-07-15 09:00:00", "finished_at": "2026-07-15 09:00:30",
             "status": "error", "new_events": 0, "error": "boom", "stats_json": None}]
    text = formatters.format_scan_history(runs)
    assert "❌ #7" in text and "ошибка: boom" in text and "30 c" in text


def test_notification_recipients_admin_plus_subscribers(tmp_db):
    # Админ всегда первый; подписчики добавляются без дублей.
    assert bot.notification_recipients() == [111]
    db.add_subscriber(555, "Иван")
    db.add_subscriber(777)
    assert bot.notification_recipients() == [111, 555, 777]
    db.add_subscriber(111)  # админ подписался сам на себя — не дублируется
    assert bot.notification_recipients() == [111, 555, 777]


@pytest.mark.asyncio
async def test_pin_report_replaces_previous_pin(tmp_db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import main as app_main
    fake_bot = SimpleNamespace(pin_chat_message=AsyncMock(), unpin_chat_message=AsyncMock())
    await app_main._pin_report(fake_bot, 111, 42)
    fake_bot.pin_chat_message.assert_awaited_once()
    assert fake_bot.pin_chat_message.await_args.kwargs["message_id"] == 42
    assert fake_bot.pin_chat_message.await_args.kwargs["disable_notification"] is True
    fake_bot.unpin_chat_message.assert_not_awaited()
    assert db.get_setting("pinned_report_111") == "42"
    # Следующий закреп снимает предыдущий.
    await app_main._pin_report(fake_bot, 111, 77)
    assert fake_bot.unpin_chat_message.await_args.kwargs["message_id"] == 42
    assert db.get_setting("pinned_report_111") == "77"


@pytest.mark.asyncio
async def test_send_html_message_returns_message(tmp_db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    sent = SimpleNamespace(message_id=123)
    fake_bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))
    message = await bot.send_html_message(fake_bot, 111, "<b>отчёт</b>")
    assert message is sent, "send_html_message возвращает сообщение для закрепления"


@pytest.mark.asyncio
async def test_send_html_message_silent_flag(tmp_db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    fake_bot = SimpleNamespace(send_message=AsyncMock())
    await bot.send_html_message(fake_bot, 111, "<b>отчёт без событий</b>", silent=True)
    assert fake_bot.send_message.await_args.kwargs["disable_notification"] is True
    await bot.send_html_message(fake_bot, 111, "<b>событие!</b>")
    assert fake_bot.send_message.await_args.kwargs["disable_notification"] is False


def test_daily_summary_data_and_format(tmp_db):
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust (SPY)", "ETF")
    db.remember_article("https://x.com/1", matcher.url_hash("https://x.com/1"), None, None,
                        "SEC", "skipped:no_keywords", status="keyword_filtered", title="Routine filing")
    db.remember_article("https://x.com/2", matcher.url_hash("https://x.com/2"), None, None,
                        "Tavily", "rejected:не событие", status="rejected", title="Market commentary")
    db.save_event({"fingerprint": "fp-1", "asset_code": "SPYF", "event_type": "stock_split",
                   "title": "Split announced", "summary_ru": "x", "source_url": "https://x.com/3",
                   "confidence": 0.9})
    data = db.daily_summary_data()
    assert data["status_counts"]["keyword_filtered"] == 1
    assert len(data["llm_seen"]) == 1 and data["llm_seen"][0]["title"] == "Market commentary"
    assert len(data["events"]) == 1

    text = formatters.format_daily_summary(data, narrative="Спокойный день, одно событие по SPYF.")
    assert "🗞 <b>Итоги дня мониторинга</b>" in text
    assert "Спокойный день, одно событие по SPYF." in text
    assert "🔴 SPYF — Дробление акций" in text
    assert "• прошло до Gemini: 1" in text
    assert "• без признаков события: 1" in text
    # Без narrative (Gemini недоступен) — только цифры, без падения.
    assert "Цифры за 24 часа" in formatters.format_daily_summary(data, None)


@pytest.mark.asyncio
async def test_build_daily_summary_with_fake_llm(tmp_db, monkeypatch):
    from app import main as app_main
    from app.llm import LLMUsage

    class FakeLLM:
        name, model = "fake", "fake-1"
        async def generate(self, prompt, max_output_tokens=1024):
            assert "Данные дня" in prompt
            return "Сводка от модели.", LLMUsage(calls=1, input_tokens=10, output_tokens=5, cost_usd=0.001)

    monkeypatch.setattr(app_main.monitor, "llm", FakeLLM())
    text = await app_main.build_daily_summary()
    assert "Сводка от модели." in text and "Цифры за 24 часа" in text
    assert db.cost_stats()["today"]["calls"] == 1, "использование записано в /cost"


@pytest.mark.asyncio
async def test_summary_toggle(tmp_db):
    from app import main as app_main
    update = make_update()
    await app_main.summary_cmd(update, make_context(["off"]))
    assert db.get_setting(app_main.DAILY_SUMMARY_KEY) == "0"
    update2 = make_update()
    await app_main.summary_cmd(update2, make_context(["on"]))
    assert db.get_setting(app_main.DAILY_SUMMARY_KEY) == "1"


@pytest.mark.asyncio
async def test_reports_denied_for_non_admin(tmp_db):
    db.add_subscriber(555)
    for handler in (bot.scans_history, bot.articles):
        update = make_update(user_id=555)
        await handler(update, make_context([]))
        assert "Нет доступа" in _text(update), handler.__name__


@pytest.mark.asyncio
async def test_reports_denied_for_stranger(tmp_db):
    update = make_update(user_id=999)
    await bot.reports_cmd(update, make_context(["off"]))
    assert "Нет доступа" in _text(update)
    assert db.get_setting(f"{bot.SCAN_REPORTS_KEY}:999", "") == ""


@pytest.mark.asyncio
async def test_scheduled_scan_respects_personal_reports(tmp_db, monkeypatch):
    """Плановая проверка без событий: сводку получают только те, у кого отчёты включены."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import main as app_main
    db.add_subscriber(555)
    db.add_subscriber(777)
    await bot.reports_cmd(make_update(user_id=555), make_context(["off"]))

    sent: list[int] = []

    async def fake_send(bot_, chat_id, text, **kwargs):
        sent.append(chat_id)
        return SimpleNamespace(message_id=1)

    found: list = []

    async def fake_scan(**kwargs):
        return list(found)

    monkeypatch.setattr(app_main, "send_html_message", fake_send)
    monkeypatch.setattr(app_main.monitor, "scan_all", fake_scan)
    monkeypatch.setattr(app_main, "send_events", AsyncMock())
    monkeypatch.setattr(app_main, "notify_health", AsyncMock())
    monkeypatch.setattr(app_main, "_pin_report", AsyncMock())
    context = SimpleNamespace(application=SimpleNamespace(bot=object()))

    await app_main.scheduled_scan(context)
    assert sent == [bot.settings.admin_telegram_id, 777]

    sent.clear()
    found.append({"asset_code": "SPYF", "event_type": "stock_split", "title": "x", "id": 1})
    await app_main.scheduled_scan(context)
    assert sent == [bot.settings.admin_telegram_id, 555, 777], "с событиями — всем"
