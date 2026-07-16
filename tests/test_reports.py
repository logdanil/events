"""Отчёты о проверках (/reports), история (/scans), просмотр публикаций (/articles)."""
from __future__ import annotations

import json

import pytest

from app import bot, db, formatters, matcher
from tests.test_bot import make_context, make_update


def _text(update) -> str:
    return "\n".join(c.args[0] for c in update.effective_message.reply_text.await_args_list)


@pytest.mark.asyncio
async def test_reports_toggle(tmp_db):
    update = make_update()
    await bot.reports_cmd(update, make_context([]))
    assert "включены" in _text(update), "отчёты о проверках включены по умолчанию"
    update2 = make_update()
    await bot.reports_cmd(update2, make_context(["on"]))
    assert db.get_setting(bot.SCAN_REPORTS_KEY) == "1"
    assert "включены" in _text(update2)
    update3 = make_update()
    await bot.reports_cmd(update3, make_context(["выкл"]))
    assert db.get_setting(bot.SCAN_REPORTS_KEY) == "0"


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
async def test_reports_denied_for_non_admin(tmp_db):
    db.add_subscriber(555)
    for handler in (bot.reports_cmd, bot.scans_history, bot.articles):
        update = make_update(user_id=555)
        await handler(update, make_context([]))
        assert "Нет доступа" in _text(update), handler.__name__
