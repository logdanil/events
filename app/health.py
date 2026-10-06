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

# Хосты с десятками однотипных лент: сбой учитывается ОДНИМ инцидентом на хост,
# иначе /health и рассылка тонут в 70+ одинаковых предупреждениях.
AGGREGATED_HOSTS = ("news.google.com",)


def _aggregated_host(url: str) -> str | None:
    for host in AGGREGATED_HOSTS:
        if host in url:
            return host
    return None


def check_source(source: dict, error: str | None) -> dict | None:
    """Источник: оповещаем после SOURCE_FAIL_THRESHOLD подряд неудач; при первом
    успехе после серии — сообщение о восстановлении."""
    key = f"source:{source['id']}"
    host = _aggregated_host(source["url"])
    if error is None:
        with db.connect() as conn:
            conn.execute("UPDATE source_defs SET fail_streak=0 WHERE id=?", (source["id"],))
        info = resolve("source", key)
        if info:
            pending_resolved.append(info)
        if host:
            info = resolve("source", f"host:{host}")
            if info:
                pending_resolved.append(info)
        return None
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET fail_streak=COALESCE(fail_streak,0)+1 WHERE id=?",
                     (source["id"],))
        streak = conn.execute("SELECT fail_streak FROM source_defs WHERE id=?",
                              (source["id"],)).fetchone()[0]
    # 404 — страница удалена: отключаем только после длинной серии (одиночные 404
    # бывают сбоями CDN — Lumentum отдал 404 три раза и ожил), резервные каналы работают.
    if streak >= SOURCE_FAIL_THRESHOLD * 3 and "404" in error:
        with db.connect() as conn:
            conn.execute("UPDATE source_defs SET enabled=0 WHERE id=?", (source["id"],))
        resolve("source", key)
        return report("source", f"disabled:{source['id']}",
                      f"Источник «{source['name']}» отключён: страница не существует (404, {streak} раз). "
                      f"Активы: {', '.join(source.get('subscribers') or []) or 'глобальный'}. "
                      f"Если адрес изменился — добавьте новый через /addsource. URL: {source['url']}",
                      severity="info")
    if host and streak >= SOURCE_FAIL_THRESHOLD:
        with db.connect() as conn:
            failing = conn.execute(
                "SELECT COUNT(*) FROM source_defs WHERE url LIKE ? AND enabled=1 "
                "AND COALESCE(fail_streak,0) >= ?",
                (f"%{host}%", SOURCE_FAIL_THRESHOLD)).fetchone()[0]
        return report("source", f"host:{host}",
                      f"{host}: недоступны {failing} лент ({error[:120]}). Похоже на лимит "
                      f"частоты запросов — бот уже снизил темп, доступ восстановится сам.",
                      severity="warning")
    if streak >= SOURCE_FAIL_THRESHOLD:
        subscribers = ", ".join(source.get("subscribers") or []) or "глобальный"
        return report("source", key,
                      f"Источник «{source['name']}» недоступен {streak} проверок подряд "
                      f"({error[:120]}). Активы: {subscribers}. URL: {source['url']}",
                      severity="warning")
    return None


# Циркуляры LME/LME Clear через индекс Google News: сама LME блокирует
# адреса дата-центров, а её уведомления (маржа, спецификации) индексируются.
_LME_CIRCULARS_FEED = ("https://news.google.com/rss/search?q=site:lme.com+when:7d"
                       "&hl=en-US&gl=US&ceid=US:en")

# Сломанные у владельцев страницы → рабочие замены (применяется при старте).
URL_FIXES = {
    "https://www.ishares.com/uk/individual/en/products/251751/ishares-msci-emerging-markets-ucits-etf":
        "https://www.ishares.com/us/products/239637/ishares-msci-emerging-markets-etf",
    # Kraken закрыл блог для всех программных клиентов (403 с любым TLS-отпечатком,
    # даже с домашнего IP) — читаем тот же блог через ленту Google News.
    "https://blog.kraken.com/feed":
        "https://news.google.com/rss/search?q=site:blog.kraken.com+when:7d&hl=en-US&gl=US&ceid=US:en",
    # iShares удалил раздел «библиотека документов» на британском сайте (404 навсегда) —
    # переводим STOX, DAX и INDIA на живые страницы самих фондов.
    "https://www.ishares.com/uk/individual/en/education/library?productIdentifier=IE0008471009%7EBRS8PR212%7E251781":
        "https://www.ishares.com/uk/individual/en/products/251781/ishares-euro-stoxx-50-ucits-etf-inc-fund",
    "https://www.ishares.com/uk/individual/en/education/library?productIdentifier=DE0005933931%7EBRWJRG850%7E251464":
        "https://www.ishares.com/uk/individual/en/products/251464/ishares-core-dax-ucits-etf-de-fund",
    "https://www.ishares.com/uk/individual/en/education/library?productIdentifier=IE00BZCQB185%7EBRTGYM149%7E297617":
        "https://www.ishares.com/uk/individual/en/products/297617/ishares-msci-india-ucits-etf-usd-acc-fund",
    # LME блокирует адреса дата-центров на всех TLS-отпечатках (403 с августа) —
    # читаем циркуляры LME/LME Clear (маржа, торги) через индекс Google News;
    # четыре страницы металлов сводятся в одну общую ленту с четырьмя подписками.
    "https://www.lme.com/Metals/Non-ferrous/LME-Aluminium/Contract-specifications?sc_camp=4F52AC6F65094CF9B8E4DD64DB07ADBE":
        _LME_CIRCULARS_FEED,
    "https://www.lme.com/en/Metals/Non-ferrous/LME-Copper/Contract-specifications":
        _LME_CIRCULARS_FEED,
    "https://www.lme.com/Metals/Non-ferrous/LME-Zinc/Contract-specifications":
        _LME_CIRCULARS_FEED,
    "https://www.lme.com/Metals/Non-ferrous/LME-Nickel/Contract-specifications":
        _LME_CIRCULARS_FEED,
    # TRON DAO удалил раздел новостей с сайта (404), официальный Medium заброшен —
    # события фонда и сети читаем через ленту Google News.
    "https://trondao.org/news/":
        "https://news.google.com/rss/search?q=%22TRON+DAO%22+OR+trondao+when:7d&hl=en-US&gl=US&ceid=US:en",
    # iShares перестал отдавать страницы фондов по номеру без имени (403 на все
    # отпечатки) — переводим на полные адреса тех же страниц.
    "https://www.ishares.com/us/products/239680/":
        "https://www.ishares.com/us/products/239680/ishares-msci-south-africa-etf",
    "https://www.ishares.com/us/products/271542/":
        "https://www.ishares.com/us/products/271542/ishares-msci-saudi-arabia-etf",
}


# Живые страницы первоисточников взамен исчезнувших (404): подключаются при старте.
REPLACEMENT_SOURCES = {
    "HYNIX": ["https://news.skhynix.com/en/", "https://www.skhynix.com/ir/UI-FR-IR12_T1/"],
    "SAUDI": ["https://www.ishares.com/us/products/271540/ishares-msci-saudi-arabia-capped-etf"],
    "NIKK": ["https://www.blackrock.com/jp/individual-en/en/products/251897/ishares-core-nikkei-225-etf"],
}


# Страницы, ошибочно отключённые за кратковременные 404 (сбои CDN): включаются обратно.
REVIVE_SOURCES = ["https://www.lumentum.com/en/newsroom"]


def rehabilitate_sources() -> list[str]:
    """Возвращает в работу источники, отключённые старым авто-приглушением по 403
    (обход через TLS-отпечатки теперь открывает большинство из них), и чинит URL
    из URL_FIXES. Вызывается при старте. Возвращает список сообщений."""
    messages = []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key FROM health_incidents WHERE key LIKE 'muted:%'").fetchall()
        ids = [int(str(r[0]).split(":", 1)[1]) for r in rows]
        for sid in ids:
            cur = conn.execute(
                "UPDATE source_defs SET enabled=1, fail_streak=0 WHERE id=? AND enabled=0", (sid,))
            if cur.rowcount:
                name = conn.execute("SELECT name FROM source_defs WHERE id=?", (sid,)).fetchone()
                messages.append(f"источник «{name[0]}» снова в мониторинге (обход антибот-защиты)")
        conn.execute("DELETE FROM health_incidents WHERE key LIKE 'muted:%'")
        for old, new in URL_FIXES.items():
            old_row = conn.execute("SELECT id, enabled FROM source_defs WHERE url=?", (old,)).fetchone()
            if not old_row:
                continue
            new_row = conn.execute("SELECT id, enabled FROM source_defs WHERE url=?", (new,)).fetchone()
            if new_row and not old_row[1] and new_row[1]:
                # Уже починено ранее — но зависшие уведомления по старому источнику
                # всё равно закрываем (регресс TRON: инцидент «отключён» пережил замену).
                cur = conn.execute(
                    "UPDATE health_incidents SET resolved_at=? WHERE component='source' "
                    "AND key IN (?, ?) AND resolved_at IS NULL",
                    (_now(), f"source:{old_row[0]}", f"disabled:{old_row[0]}"))
                if cur.rowcount:
                    messages.append(f"закрыто зависшее уведомление по заменённому источнику: {old}")
                continue
            if new_row:
                # Рабочий URL уже есть отдельным источником: сломанный выключаем,
                # его подписки переносим на рабочий (UNIQUE по url не даёт переименовать).
                conn.execute("INSERT OR IGNORE INTO asset_sources (asset_code, source_id) "
                             "SELECT asset_code, ? FROM asset_sources WHERE source_id=?",
                             (new_row[0], old_row[0]))
                conn.execute("UPDATE source_defs SET enabled=0 WHERE id=?", (old_row[0],))
                conn.execute("UPDATE source_defs SET enabled=1, fail_streak=0 WHERE id=?",
                             (new_row[0],))
            else:
                conn.execute("UPDATE source_defs SET url=?, enabled=1, fail_streak=0 WHERE id=?",
                             (new, old_row[0]))
            # В той же транзакции: отдельное соединение (resolve) упёрлось бы в блокировку.
            # Закрываем и «недоступен» (source:), и «отключён за 404» (disabled:).
            conn.execute("UPDATE health_incidents SET resolved_at=? WHERE component='source' "
                         "AND key IN (?, ?) AND resolved_at IS NULL",
                         (_now(), f"source:{old_row[0]}", f"disabled:{old_row[0]}"))
            messages.append(f"сломанный URL заменён на рабочий: {new}")
    # Замены исчезнувших страниц — отдельными соединениями уже после транзакции выше.
    for code, urls in REPLACEMENT_SOURCES.items():
        with db.connect() as conn:
            if not conn.execute("SELECT 1 FROM assets WHERE code=?", (code,)).fetchone():
                continue
            missing = [u for u in urls if not conn.execute(
                "SELECT 1 FROM source_defs WHERE url=?", (u,)).fetchone()]
        for url in missing:
            db.add_source(code, url)
            messages.append(f"{code}: подключён рабочий первоисточник {url}")
        # Замена подключена — уведомления «страница не существует» по этому активу закрываем.
        with db.connect() as conn:
            dead_ids = [r[0] for r in conn.execute(
                "SELECT d.id FROM source_defs d JOIN asset_sources s ON s.source_id=d.id "
                "WHERE s.asset_code=? AND d.enabled=0", (code,)).fetchall()]
            for sid in dead_ids:
                cur = conn.execute(
                    "UPDATE health_incidents SET resolved_at=? WHERE component='source' "
                    "AND key=? AND resolved_at IS NULL", (_now(), f"disabled:{sid}"))
                if cur.rowcount:
                    messages.append(f"{code}: закрыто уведомление об исчезнувшей странице (id {sid})")
    with db.connect() as conn:
        for url in REVIVE_SOURCES:
            cur = conn.execute(
                "UPDATE source_defs SET enabled=1, fail_streak=0 WHERE url=? AND enabled=0", (url,))
            if cur.rowcount:
                row = conn.execute("SELECT id FROM source_defs WHERE url=?", (url,)).fetchone()
                conn.execute("UPDATE health_incidents SET resolved_at=? WHERE component='source' "
                             "AND key=? AND resolved_at IS NULL", (_now(), f"disabled:{row[0]}"))
                messages.append(f"источник возвращён в мониторинг (404 был кратковременным): {url}")
    # Интервал Google News: не чаще раза в 3 часа и вразнобой (id вносит разброс),
    # чтобы 70+ лент не приходили к Google одновременно — он банит за это 503-ми.
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE source_defs SET refresh_interval = 180 + (id * 7) % 120 "
            "WHERE url LIKE '%news.google.com%' AND refresh_interval < 120")
        if cur.rowcount:
            messages.append(f"Google News: интервал опроса {cur.rowcount} лент растянут до 3-5 часов")
        cur = conn.execute(
            "UPDATE health_incidents SET resolved_at=? WHERE component='source' "
            "AND resolved_at IS NULL AND key LIKE 'source:%' AND message LIKE '%news.google.com%'",
            (_now(),))
        if cur.rowcount:
            messages.append(f"закрыты {cur.rowcount} отдельных уведомлений Google News (теперь одно общее)")
    # Сетевые сбои Telegram — временные; старые записи о них из «открытых проблем» убираем.
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE health_incidents SET resolved_at=? WHERE component='bot' AND resolved_at IS NULL "
            "AND (key LIKE 'handler:NetworkError%' OR key LIKE 'handler:TimedOut%' "
            "OR key LIKE 'handler:RetryAfter%' OR key LIKE 'handler:Conflict%')", (_now(),))
        if cur.rowcount:
            messages.append("закрыты записи о временных сетевых сбоях Telegram")
    return messages


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
    emoji = SEVERITY_EMOJI.get(alert.get("severity"), "⚠️")
    head = "Новая проблема" if alert.get("new") else f"Проблема сохраняется (×{alert.get('occurrences', 1)})"
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
