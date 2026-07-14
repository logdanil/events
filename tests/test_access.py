"""Ролевая модель: администратор — всё; подписчики — уведомления и чтение событий."""
from __future__ import annotations

import pytest

from app import bot, db
from tests.test_bot import ADMIN_ID, make_context, make_update
from tests.test_ux_commands import _seed_event

EMPLOYEE_ID = 555


def _text(update) -> str:
    return "\n".join(c.args[0] for c in update.effective_message.reply_text.await_args_list)


def test_subscriber_crud(tmp_db):
    assert db.add_subscriber(EMPLOYEE_ID, "Иван") is True
    assert db.add_subscriber(EMPLOYEE_ID) is False, "повторное добавление не дублирует"
    assert db.is_subscriber(EMPLOYEE_ID) is True
    assert [s["chat_id"] for s in db.list_subscribers()] == [EMPLOYEE_ID]
    assert db.remove_subscriber(EMPLOYEE_ID) is True
    assert db.is_subscriber(EMPLOYEE_ID) is False


@pytest.mark.asyncio
async def test_admin_manages_users(tmp_db):
    update = make_update()
    await bot.add_user(update, make_context([str(EMPLOYEE_ID), "Иван", "Петров"]))
    assert "подписан на уведомления" in _text(update)
    assert db.is_subscriber(EMPLOYEE_ID)
    update2 = make_update()
    await bot.users(update2, make_context([]))
    assert "Иван Петров" in _text(update2)
    update3 = make_update()
    await bot.remove_user(update3, make_context([str(EMPLOYEE_ID)]))
    assert not db.is_subscriber(EMPLOYEE_ID)


@pytest.mark.asyncio
async def test_non_admin_cannot_manage_users(tmp_db):
    db.add_subscriber(EMPLOYEE_ID)
    update = make_update(user_id=EMPLOYEE_ID)
    await bot.add_user(update, make_context(["777"]))
    assert "Нет доступа" in _text(update)
    assert not db.is_subscriber(777)


@pytest.mark.asyncio
async def test_subscriber_can_read_events_but_not_manage(tmp_db):
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust (SPY)", "ETF")
    _seed_event("SPYF", "Дробление SPYF")
    db.add_subscriber(EMPLOYEE_ID)
    # Чтение: /events, /event, /digest доступны.
    for handler, args in ((bot.events_history, []), (bot.show_event, ["SPYF"]),
                          (bot.digest, [])):
        update = make_update(user_id=EMPLOYEE_ID)
        await handler(update, make_context(args))
        assert "Нет доступа" not in _text(update), handler.__name__
    # Управление: /scan-подобные команды закрыты.
    for handler, args in ((bot.add_asset, ["X", "|", "ETF", "|", "Name"]),
                          (bot.remove_asset, ["SPYF"]),
                          (bot.set_interval, ["60"]),
                          (bot.dbstats, []),
                          (bot.rematch, [])):
        update = make_update(user_id=EMPLOYEE_ID)
        await handler(update, make_context(args))
        assert "Нет доступа" in _text(update), handler.__name__


@pytest.mark.asyncio
async def test_stranger_gets_hint(tmp_db):
    update = make_update(user_id=999)
    await bot.events_history(update, make_context([]))
    assert "/adduser" in _text(update)


@pytest.mark.asyncio
async def test_start_shows_role_help(tmp_db):
    db.add_subscriber(EMPLOYEE_ID)
    admin = make_update()
    await bot.start(admin, make_context([]))
    assert "/adduser" in _text(admin)
    viewer = make_update(user_id=EMPLOYEE_ID)
    await bot.start(viewer, make_context([]))
    text = _text(viewer)
    assert "Вы подписаны на уведомления" in text
    assert "/debugscan" not in text
