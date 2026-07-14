from __future__ import annotations

import pytest

from app import bot, db
from tests.test_bot import make_context, make_update


@pytest.mark.asyncio
async def test_dbstats_command(tmp_db):
    update = make_update()
    await bot.dbstats(update, make_context([]))
    text = update.effective_message.reply_text.await_args.args[0]
    assert "Путь:" in text and "Активов: 0" in text and "Запусков сканирования: 0" in text


@pytest.mark.asyncio
async def test_testevent_writes_sends_and_detects_duplicate(tmp_db):
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR")
    update = make_update()
    await bot.test_event(update, make_context(["TSM"]))
    replies = [c.args[0] for c in update.effective_message.reply_text.await_args_list]
    assert any("Записано: id=" in r for r in replies)
    assert db.db_stats()["events"] == 1
    assert db.unsent_events() == [], "тестовое событие должно быть помечено sent_at"

    update2 = make_update()
    await bot.test_event(update2, make_context(["TSM"]))
    text = update2.effective_message.reply_text.await_args.args[0]
    assert "Дубликат" in text
    assert db.db_stats()["events"] == 1


@pytest.mark.asyncio
async def test_testreport_renders_full_report_without_saving(tmp_db):
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust", "ETF", aliases=["SPY"])
    update = make_update()
    await bot.test_report(update, make_context(["SPYF"]))
    replies = [c.args[0] for c in update.effective_message.reply_text.await_args_list]
    assert "Демонстрационный отчёт (в базу не записывается):" in replies[0]
    report = "\n".join(replies[1:])
    assert "🚨 <b>ДРОБЛЕНИЕ АКЦИЙ</b>" in report
    assert "<b>Важность:</b> 🔴 Критическая" in report
    assert "<b>Дата события:</b>" in report and "<b>Дата публикации:</b>" in report
    for label in ("Коэффициент дробления", "Ex-date", "Record date", "Дата вступления в силу"):
        assert f"• {label}:" in report
    assert "<b>Возможное влияние</b>" in report
    assert "⚠️ Проверить необходимость корректировки параметров контракта." in report
    assert "1. <a href=" in report and "2. <a href=" in report
    assert "<b>Достоверность:</b> 94%" in report
    assert db.db_stats()["events"] == 0, "демонстрационный отчёт не должен записываться в базу"


@pytest.mark.asyncio
async def test_testreport_denied_for_non_admin(tmp_db):
    update = make_update(user_id=999)
    await bot.test_report(update, make_context(["SPYF"]))
    update.effective_message.reply_text.assert_awaited_once_with("Нет доступа.")


@pytest.mark.asyncio
async def test_testevent_denied_for_non_admin(tmp_db):
    update = make_update(user_id=999)
    await bot.test_event(update, make_context(["TSM"]))
    update.effective_message.reply_text.assert_awaited_once_with("Нет доступа.")
    assert db.db_stats()["events"] == 0
