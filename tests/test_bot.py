from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import bot, db

ADMIN_ID = 111  # совпадает с admin_telegram_id из фикстуры tmp_db


def make_update(user_id: int = ADMIN_ID):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )


def make_context(args: list[str]):
    application = SimpleNamespace(job_queue=MagicMock(), bot_data={})
    return SimpleNamespace(args=args, application=application)


@pytest.mark.asyncio
async def test_guard_rejects_non_admin(tmp_db):
    update = make_update(user_id=999)
    assert await bot.guard(update) is False
    update.effective_message.reply_text.assert_awaited_once_with("Нет доступа.")
    assert await bot.guard(make_update()) is True


@pytest.mark.asyncio
async def test_add_asset_command(tmp_db):
    update = make_update()
    await bot.add_asset(update, make_context(["TSM", "|", "ADR", "|", "Taiwan", "Semiconductor", "ADR"]))
    asset = db.get_asset("TSM")
    assert asset is not None
    assert asset["instrument_type"] == "ADR"
    assert asset["name"] == "Taiwan Semiconductor ADR"


@pytest.mark.asyncio
async def test_add_asset_rejects_bad_format(tmp_db):
    update = make_update()
    await bot.add_asset(update, make_context(["TSM", "|", "WRONGTYPE", "|", "Name"]))
    assert db.get_asset("TSM") is None
    text = update.effective_message.reply_text.await_args.args[0]
    assert text.startswith("Формат: /add CODE | TYPE | NAME")


@pytest.mark.asyncio
async def test_add_source_command(tmp_db):
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR")
    update = make_update()
    await bot.add_source(update, make_context(["TSM", "|", "https://investor.tsmc.com/english"]))
    asset = db.get_asset("TSM")
    assert [s["url"] for s in asset["sources"]] == ["https://investor.tsmc.com/english"]
    # Повторное добавление того же источника не создаёт дубликат.
    await bot.add_source(make_update(), make_context(["TSM", "|", "https://investor.tsmc.com/english"]))
    assert len(db.get_asset("TSM")["sources"]) == 1


@pytest.mark.asyncio
async def test_add_source_requires_existing_asset(tmp_db):
    update = make_update()
    await bot.add_source(update, make_context(["NOPE", "|", "https://example.com"]))
    text = update.effective_message.reply_text.await_args.args[0]
    assert "не найден" in text


@pytest.mark.asyncio
async def test_interval_reschedules_job(tmp_db):
    update = make_update()
    context = make_context(["45"])
    old_job = MagicMock()
    context.application.job_queue.get_jobs_by_name.return_value = [old_job]
    context.application.bot_data["scheduled_scan"] = object()

    await bot.set_interval(update, context)

    assert db.get_setting("interval_minutes") == "45"
    old_job.schedule_removal.assert_called_once()
    context.application.job_queue.run_repeating.assert_called_once_with(
        context.application.bot_data["scheduled_scan"], interval=45 * 60, first=45 * 60, name="scheduled_scan")


@pytest.mark.asyncio
async def test_interval_rejects_too_small(tmp_db):
    update = make_update()
    context = make_context(["5"])
    await bot.set_interval(update, context)
    context.application.job_queue.run_repeating.assert_not_called()
    update.effective_message.reply_text.assert_awaited_once_with("Укажите число минут не меньше 15: /interval 60")
