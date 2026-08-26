"""Контроль здоровья: инциденты, дедупликация, источники, планировщик, доставка."""
from __future__ import annotations

import pytest

from app import bot, db, health
from app.monitor import ScanStats
from tests.test_bot import make_context, make_update


def _text(update) -> str:
    return "\n".join(c.args[0] for c in update.effective_message.reply_text.await_args_list)


def test_report_dedup_and_resolve(tmp_db):
    a1 = health.report("llm", "unavailable", "Gemini 503", "critical")
    assert a1 and a1["new"] is True
    # Повтор в течение cooldown — молчим, но считаем.
    assert health.report("llm", "unavailable", "Gemini 503", "critical") is None
    incidents = health.open_incidents()
    assert len(incidents) == 1 and incidents[0]["occurrences"] == 2
    # Восстановление — сообщаем (администратор был оповещён).
    r = health.resolve("llm", "unavailable")
    assert r and r["component"] == "llm"
    assert health.open_incidents() == []
    # Повторное восстановление — тишина.
    assert health.resolve("llm", "unavailable") is None
    # Новое падение после восстановления — снова «новая проблема».
    a2 = health.report("llm", "unavailable", "Gemini 503", "critical")
    assert a2 and a2["new"] is True


def test_source_alert_after_threshold(tmp_db):
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust", "ETF")
    db.add_source("SPYF", "https://ir.example.com/news")
    source = next(s for s in db.list_source_defs() if "ir.example.com" in s["url"])
    # Первые сбои — тишина (единичная ошибка сети не повод будить).
    for _ in range(health.SOURCE_FAIL_THRESHOLD - 1):
        assert health.check_source(source, "HTTP 503") is None
    alert = health.check_source(source, "HTTP 503")
    assert alert and "недоступен" in alert["message"] and "SPYF" in alert["message"]
    # Успех — сброс серии и «восстановлено» в буфере.
    health.pending_resolved.clear()
    assert health.check_source(source, None) is None
    assert health.pending_resolved and health.pending_resolved[0]["component"] == "source"
    with db.connect() as conn:
        assert conn.execute("SELECT fail_streak FROM source_defs WHERE id=?",
                            (source["id"],)).fetchone()[0] == 0


def test_scan_totals_checks(tmp_db):
    stats = ScanStats(sources_checked=100, publications=0)
    alerts = health.check_scan_totals(stats)
    assert any(a["key"] == "zero_publications" and a["severity"] == "critical" for a in alerts)
    stats2 = ScanStats(sources_checked=100, publications=1500, llm_failed=7, cost_usd=0.9)
    alerts2 = health.check_scan_totals(stats2)
    keys = {a["key"] for a in alerts2}
    assert "batch_failed" in keys and "cost_spike" in keys
    # zero_publications при этом закрывается.
    assert all(i["key"] != "zero_publications" for i in health.open_incidents())


def test_scheduler_stale(tmp_db):
    run_id = db.start_scan_run()
    db.finish_scan_run(run_id, "ok", 50, 0)
    assert health.check_scheduler(60) is None, "свежая проверка — всё в порядке"
    with db.connect() as conn:
        conn.execute("UPDATE scan_runs SET finished_at=datetime('now','-5 hours') WHERE id=?", (run_id,))
    alert = health.check_scheduler(60)
    assert alert and alert["key"] == "stale" and alert["severity"] == "critical"


def test_delivery_blocked_user_is_info(tmp_db):
    a = health.check_delivery("telegram", "555", RuntimeError("Forbidden: bot was blocked by the user"))
    assert a and a["severity"] == "info" and "заблокировал" in a["message"]
    b = health.check_delivery("telegram", "556", RuntimeError("Timed out"))
    assert b and b["severity"] == "warning"


def test_health_report_and_format(tmp_db):
    health.report("llm", "unavailable", "Gemini 503", "critical")
    text = health.format_health_report(60)
    assert "Здоровье сервиса" in text and "Открытые проблемы (1)" in text and "Gemini 503" in text
    health.resolve("llm", "unavailable")
    assert "Открытых проблем нет" in health.format_health_report(60)
    alert = {"component": "llm", "severity": "critical", "message": "x", "occurrences": 3, "new": False}
    assert "×3" in health.format_alert(alert)


@pytest.mark.asyncio
async def test_export_import_recipients(tmp_db):
    db.add_subscriber(555, "Иван Петров")
    db.add_subscriber(777)
    db.add_email_recipient("a@x.ru")
    update = make_update()
    await bot.export_recipients(update, make_context([]))
    text = _text(update)
    assert "/importusers 555:Иван_Петров 777" in text
    assert "/addemail a@x.ru" in text
    # Восстановление в пустую базу.
    db.remove_subscriber(555); db.remove_subscriber(777)
    update2 = make_update()
    await bot.import_users(update2, make_context(["555:Иван_Петров", "777", "мусор"]))
    assert "добавлено: 2" in _text(update2)
    subs = {s["chat_id"]: s["name"] for s in db.list_subscribers()}
    assert subs == {555: "Иван Петров", 777: None}


def test_rehabilitate_muted_sources(tmp_db):
    """Источники, отключённые старым авто-приглушением, возвращаются в мониторинг."""
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR")
    db.add_source("TSM", "https://pr.tsmc.com/english/news")
    src = db.list_source_defs()[0]
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET enabled=0, fail_streak=16 WHERE id=?", (src["id"],))
        conn.execute("INSERT INTO health_incidents (component, key, message, severity, "
                     "first_seen, last_seen) VALUES ('source', ?, 'отключён', 'info', "
                     "'2026-08-19', '2026-08-19')", (f"muted:{src['id']}",))
    messages = health.rehabilitate_sources()
    assert messages and "снова в мониторинге" in messages[0]
    fresh = db.list_source_defs()[0]
    assert fresh["enabled"] == 1 and not fresh.get("fail_streak")
    assert health.rehabilitate_sources() == [], "повторный вызов ничего не трогает"


def test_403_source_stays_enabled(tmp_db):
    """403-источник не отключается: продолжаем проверять (обход по TLS-отпечаткам)."""
    db.upsert_asset("SONY", "Sony Group Corporation", "ADR")
    db.add_source("SONY", "https://blocked.example.com/press")
    db.add_source("SONY", "https://backup.example.com/rss")
    blocked = next(s for s in db.list_source_defs() if "blocked" in s["url"])
    for _ in range(health.SOURCE_FAIL_THRESHOLD * 5):
        health.check_source(blocked, "HTTPStatusError: Client error '403 Forbidden'")
    fresh = next(s for s in db.list_source_defs(enabled_only=False) if s["id"] == blocked["id"])
    assert fresh["enabled"] == 1


def test_url_fix_with_existing_duplicate(tmp_db, monkeypatch):
    """Если рабочий URL уже есть в базе, сломанный источник выключается,
    подписки переезжают — и это не роняет запуск (регресс краш-цикла на Railway)."""
    db.upsert_asset("EM", "iShares MSCI Emerging Markets ETF", "ETF")
    db.add_source("EM", "https://broken.example.com/page")
    db.add_source("EM", "https://working.example.com/page")
    monkeypatch.setattr(health, "URL_FIXES",
                        {"https://broken.example.com/page": "https://working.example.com/page"})
    messages = health.rehabilitate_sources()
    assert messages and "заменён на рабочий" in messages[0]
    rows = {s["url"]: s for s in db.list_source_defs(enabled_only=False)}
    assert rows["https://broken.example.com/page"]["enabled"] == 0
    assert rows["https://working.example.com/page"]["enabled"] == 1
    assert health.rehabilitate_sources() == []


def test_url_fix_with_open_incident_same_transaction(tmp_db, monkeypatch):
    """Регресс Railway: у сломанного источника есть открытый инцидент — реабилитация
    не должна упираться в блокировку базы (второе соединение внутри транзакции)."""
    db.upsert_asset("EM", "iShares MSCI Emerging Markets ETF", "ETF")
    db.add_source("EM", "https://broken.example.com/page")
    db.add_source("EM", "https://working.example.com/page")
    broken = next(s for s in db.list_source_defs() if "broken" in s["url"])
    health.report("source", f"source:{broken['id']}", "недоступен 94 проверок")
    monkeypatch.setattr(health, "URL_FIXES",
                        {"https://broken.example.com/page": "https://working.example.com/page"})
    messages = health.rehabilitate_sources()
    assert messages, "реабилитация должна применяться при открытом инциденте"
    assert all(i["key"] != f"source:{broken['id']}" for i in health.open_incidents())
