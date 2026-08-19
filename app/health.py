"""Контроль здоровья конвейера: единая точка регистрации инцидентов и правил оповещения.

Каждый этап (источники, matcher, LLM, доставка, планировщик) сообщает сюда об
успехе или ошибке. Модуль решает, когда это стоит внимания администратора:
- источник: N подряд неудачных опросов (единичный сбой сети — не повод);
- LLM: любой сбой батча (кандидаты не проанализированы) или полная недоступность;
- планировщик: проверка не запускалась дольше 2× интервала («тихая смерть»);
- доставка (Telegram/почта): ошибки, кроме «пользователь заблокировал бота»;
- аномалии данных: 0 публикаций от всех источников, резкий рост стоимости.

Оповещения дедуплицируются: об одной проблеме — не чаще раза в COOLDOWN_HOURS,
пока она не решится; при восстановлении приходит сообщение «✅ восстановлено».
Всё хранится в SQLite (таблица health_incidents), поэтому переживает рестарты.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app import db

log = logging.getLogger(__name__)

COOLDOWN_HOURS = 6                 # повтор оповещения по той же проблеме
SOURCE_FAIL_THRESHOLD = 3          # подряд неудачных опросов источника
SCHEDULER_STALE_FACTOR = 2.5       # проверка «пропала», если тише 2.5× интервала
COST_SPIKE_USD = 0.50              # стоимость одной проверки, после которой стоит насторожиться

SEVERITY_EMOJI = {"critical": "🛑", "warning": "⚠️", "info": "ℹ️"}

# Алерты и «восстановления», накопленные во время проверки (источники опрашиваются
# параллельно и не имеют доступа к Telegram) — отправляются после скана.
pending_alerts: list[dict] = []
pending_resolved: list[dict] = []


def drain() -> tuple[list[dict], list[dict]]:
    alerts, resolved = list(pending_alerts), list(pending_resolved)
    pending_alerts.clear()
    pending_resolved.clear()
    return alerts, resolved


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- Регистрация инцидентов ---------------------------------------------------

def report(component: str, key: str, message: str, severity: str = "warning") -> dict | None:
    """Зарегистрировать проблему. Возвращает описание оповещения, если его пора
    отправить (новый инцидент или истёк cooldown), иначе None."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM health_incidents WHERE component=? AND key=?",
                           (component, key)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO health_incidents(component,key,severity,message,first_seen,last_seen,"
                "occurrences,notified_at,resolved_at) VALUES(?,?,?,?,?,?,1,?,NULL)",
                (component, key, severity, message[:500], _now(), _now(), _now()))
            return {"component": component, "key": key, "severity": severity,
                    "message": message, "occurrences": 1, "new": True}
        occurrences = (row["occurrences"] or 0) + 1
        was_resolved = row["resolved_at"] is not None
        conn.execute(
            "UPDATE health_incidents SET severity=?, message=?, last_seen=?, occurrences=?, "
            "resolved_at=NULL WHERE id=?",
            (severity, message[:500], _now(), 1 if was_resolved else occurrences, row["id"]))
        due = was_resolved or row["notified_at"] is None or conn.execute(
            "SELECT 1 FROM health_incidents WHERE id=? AND "
            "datetime(notified_at, ?) <= datetime('now')",
            (row["id"], f"+{COOLDOWN_HOURS} hours")).fetchone() is not None
        if due:
            conn.execute("UPDATE health_incidents SET notified_at=? WHERE id=?", (_now(), row["id"]))
            return {"component": component, "key": key, "severity": severity,
                    "message": message, "occurrences": 1 if was_resolved else occurrences,
                    "new": was_resolved}
        return None


def resolve(component: str, key: str) -> dict | None:
    """Отметить проблему решённой. Возвращает описание для «✅ восстановлено»,
    если администратор ранее был оповещён об этой проблеме."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM health_incidents WHERE component=? AND key=? AND resolved_at IS NULL",
            (component, key)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE health_incidents SET resolved_at=? WHERE id=?", (_now(), row["id"]))
        if row["notified_at"]:
            return {"component": component, "key": key, "message": row["message"],
                    "occurrences": row["occurrences"]}
        return None


def open_incidents() -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM health_incidents WHERE resolved_at IS NULL "
            "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
            "last_seen DESC").fetchall()]


# --- Проверки конкретных этапов -----------------------------------------------

def check_source(source: dict, error: str | None) -> dict | None:
    """Источник: оповещаем после SOURCE_FAIL_THRESHOLD подряд неудач; при первом
    успехе после серии — сообщение о восстановлении."""
    key = f"source:{source['id']}"
    if error is None:
        with db.connect() as conn:
            conn.execute("UPDATE source_defs SET fail_streak=0 WHERE id=?", (source["id"],))
        info = resolve("source", key)
        if info:
            pending_resolved.append(info)
        return None
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET fail_streak=COALESCE(fail_streak,0)+1 WHERE id=?",
                     (source["id"],))
        streak = conn.execute("SELECT fail_streak FROM source_defs WHERE id=?",
                              (source["id"],)).fetchone()[0]
    # 404 — страница удалена: после порога отключаем источник, чтобы не висел в проблемах
    # вечно (резервные каналы актива продолжают работать).
    if streak >= SOURCE_FAIL_THRESHOLD and "404" in error:
        with db.connect() as conn:
            conn.execute("UPDATE source_defs SET enabled=0 WHERE id=?", (source["id"],))
        resolve("source", key)
        return report("source", f"disabled:{source['id']}",
                      f"Источник «{source['name']}» отключён: страница не существует (404, {streak} раз). "
                      f"Активы: {', '.join(source.get('subscribers') or []) or 'глобальный'}. "
                      f"Если адрес изменился — добавьте новый через /addsource. URL: {source['url']}",
                      severity="info")
    if streak >= SOURCE_FAIL_THRESHOLD:
        subscribers = ", ".join(source.get("subscribers") or []) or "глобальный"
        return report("source", key,
                      f"Источник «{source['name']}» недоступен {streak} проверок подряд "
                      f"({error[:120]}). Активы: {subscribers}. URL: {source['url']}",
                      severity="warning")
    return None


def check_scan_totals(stats) -> list[dict]:
    """Аномалии на уровне всей проверки."""
    alerts = []
    if stats.sources_checked and stats.publications == 0:
        a = report("pipeline", "zero_publications",
                   f"Все {stats.sources_checked} источников вернули 0 публикаций — "
                   "похоже на сетевую проблему сервера или массовую блокировку.",
                   severity="critical")
    else:
        a = resolve("pipeline", "zero_publications")
    if a: alerts.append(a)

    if stats.llm_failed:
        a = report("llm", "batch_failed",
                   f"Gemini не обработал {stats.llm_failed} публикаций (сбой батчей). "
                   "Они повторятся в следующем скане; если повторяется — проверьте ключ/квоту.",
                   severity="critical")
    else:
        a = resolve("llm", "batch_failed")
    if a: alerts.append(a)

    if stats.cost_usd >= COST_SPIKE_USD:
        a = report("llm", "cost_spike",
                   f"Стоимость одной проверки ${stats.cost_usd:.2f} превысила порог ${COST_SPIKE_USD:.2f}.",
                   severity="warning")
        if a: alerts.append(a)

    if stats.errors:
        a = report("pipeline", "scan_errors",
                   f"Проверка завершилась с {stats.errors} ошибками этапов (см. логи).",
                   severity="warning")
    else:
        a = resolve("pipeline", "scan_errors")
    if a: alerts.append(a)
    return alerts


def check_scheduler(interval_minutes: int) -> dict | None:
    """«Тихая смерть» планировщика: последняя проверка старше 2.5× интервала."""
    last = db.last_ok_run()
    if not last:
        return None
    with db.connect() as conn:
        stale = conn.execute(
            "SELECT datetime(?, ?) < datetime('now')",
            (last["finished_at"] or last["started_at"],
             f"+{int(interval_minutes * SCHEDULER_STALE_FACTOR)} minutes")).fetchone()[0]
    if stale:
        return report("scheduler", "stale",
                      f"Плановые проверки не выполнялись дольше {int(interval_minutes * SCHEDULER_STALE_FACTOR)} мин "
                      f"(последняя: {last['finished_at'] or last['started_at']} UTC).",
                      severity="critical")
    return resolve("scheduler", "stale")


def check_delivery(channel: str, target: str, error: Exception | None) -> dict | None:
    """Доставка в Telegram/почту. Блокировка бота пользователем — не инцидент сервиса."""
    key = f"{channel}:{target}"
    if error is None:
        return resolve("delivery", key)
    text = str(error)
    if "blocked by the user" in text or "chat not found" in text.lower():
        return report("delivery", key,
                      f"{channel}: получатель {target} недоступен (заблокировал бота или не начинал диалог). "
                      f"Удалите его из рассылки или попросите написать боту /start.",
                      severity="info")
    return report("delivery", key, f"{channel}: не удалось доставить получателю {target}: {text[:150]}",
                  severity="warning")


def check_llm_health(ok: bool, error: str | None = None) -> dict | None:
    if ok:
        return resolve("llm", "unavailable")
    return report("llm", "unavailable", f"LLM недоступна: {(error or '')[:200]}", severity="critical")


# --- Форматирование ---------------------------------------------------------------

def format_alert(alert: dict) -> str:
    emoji = SEVERITY_EMOJI.get(alert["severity"], "⚠️")
    head = "Новая проблема" if alert.get("new") else f"Проблема сохраняется (×{alert['occurrences']})"
    return (f"{emoji} <b>{head}: {alert['component']}</b>\n{alert['message']}\n\n"
            f"Все открытые проблемы: /health")


def format_resolved(info: dict) -> str:
    return f"✅ <b>Восстановлено: {info['component']}</b>\n{info['message'][:200]}"


def format_health_report(interval_minutes: int) -> str:
    incidents = open_incidents()
    last = db.last_ok_run()
    lines = ["🩺 <b>Здоровье сервиса</b>", ""]
    lines.append("Последняя успешная проверка: "
                 + (f"{last['finished_at'] or last['started_at']} UTC" if last else "ещё не было"))
    with db.connect() as conn:
        failing = conn.execute(
            "SELECT COUNT(*) FROM source_defs WHERE enabled=1 AND COALESCE(fail_streak,0) >= ?",
            (SOURCE_FAIL_THRESHOLD,)).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM source_defs WHERE enabled=1").fetchone()[0]
    lines.append(f"Источники: {total - failing}/{total} доступны"
                 + (f", {failing} стабильно недоступны" if failing else ""))
    lines.append(f"Интервал проверок: {interval_minutes} мин")
    lines.append("")
    if not incidents:
        lines.append("Открытых проблем нет ✅")
    else:
        lines.append(f"<b>Открытые проблемы ({len(incidents)}):</b>")
        for i in incidents[:15]:
            emoji = SEVERITY_EMOJI.get(i["severity"], "⚠️")
            lines.append(f"{emoji} [{i['component']}] {i['message'][:160]}"
                         f"\n   с {i['first_seen']} UTC, повторов: {i['occurrences']}")
    return "\n".join(lines)
