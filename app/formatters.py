"""Форматирование Telegram-сообщений: события, сводки, дайджесты.

Модуль не обращается к базе и внешним API: на вход — словари события/актива
(в форме строк таблицы events), на выход — HTML-текст для parse_mode="HTML".
Используются только теги <b>, <i>, <code>, <a>; всё остальное экранируется.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

MSK = ZoneInfo("Europe/Moscow")
TELEGRAM_LIMIT = 3900

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# Канонические snake_case-коды → русские названия.
EVENT_TYPE_RU = {
    "stock_split": "Дробление акций",
    "reverse_split": "Обратное дробление",
    "dividend": "Дивиденд",
    "special_dividend": "Специальный дивиденд",
    "delisting": "Делистинг",
    "trading_suspension": "Приостановка торгов",
    "merger": "Слияние",
    "acquisition": "Поглощение",
    "spin_off": "Выделение бизнеса",
    "adr_ratio_change": "Изменение коэффициента ADR",
    "symbol_change": "Изменение тикера",
    "isin_change": "Изменение ISIN",
    "etf_liquidation": "Ликвидация ETF",
    "methodology_change": "Изменение методологии фонда",
    "index_rebalance": "Ребалансировка индекса",
    "contract_specification_change": "Изменение спецификации контракта",
    "bankruptcy": "Банкротство",
    "tender_offer": "Тендерное предложение",
    "rights_issue": "Размещение прав",
    "share_issuance": "Дополнительная эмиссия",
    "listing": "Листинг",
    "conversion": "Конвертация",
    "capital_return": "Возврат капитала",
    "name_change": "Изменение наименования",
}

SEVERITY_LABEL = {
    "CRITICAL": "🔴 Критическая",
    "HIGH": "🟠 Высокая",
    "MEDIUM": "🟡 Средняя",
    "LOW": "🟢 Низкая",
}
SEVERITY_EMOJI = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}

STATUS_RU = {
    "confirmed": "Подтверждено официальным источником",
    "reported": "Сообщается в СМИ",
    "preliminary": "Предварительная информация",
    "rumor": "Неподтверждённая информация",
}

# Классификация важности по ключевым словам в типе события (русский перечень
# методологии содержит английские подсказки в скобках — учитываем и их).
_SEVERITY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("CRITICAL", (
        "split", "дроблени", "консолидац", "delisting", "делистинг",
        "ликвидац", "liquidation", "прекращение", "банкрот", "bankrupt",
        "слияни", "merger", "поглощен", "acquisition", "spin-off", "spin_off", "выделени",
        "adr-ratio", "adr_ratio", "isin", "торгового кода", "тикер", "symbol",
        "приостановка торгов", "suspension", "потери стоимости", "worthless",
        "фиксинг", "конвертац", "расчётной цены", "settlement", "размера контракта",
        "lot size", "единиц измерения", "масштабов цены", "базовой валюты",
        "redenomination", "экспирац", "спецификаци",
    )),
    ("HIGH", (
        "tender", "выкуп", "rights issue", "дополнительный выпуск", "bonus",
        "эмисси", "методологи", "methodology", "принципов управления",
        "индекс", "index", "бенчмарк", "benchmark", "алгоритма определения цены",
        "возврат капитала", "capital distribution", "distribution", "multiplier",
        "наименовани", "запуск нового", "базис",
    )),
    ("MEDIUM", (
        "дивиденд", "dividend", "отчётност", "earnings", "собрани", "meeting",
        "guidance", "маржинальн", "margin", "шаге цены", "шаг цены", "tick size",
        "торгового лота", "торговый лот", "торгового режима", "расписани",
        "логики торговых систем", "претензии", "номинальной стоимости",
        "клиринг", "расчётов",
    )),
]


def classify_severity(event_type: str | None, provided: str | None = None) -> str:
    """Важность события: валидное значение модели приоритетно, иначе — по типу."""
    if provided and str(provided).strip().upper() in SEVERITY_LABEL:
        return str(provided).strip().upper()
    t = (event_type or "").lower()
    for severity, keywords in _SEVERITY_RULES:
        if any(k in t for k in keywords):
            return severity
    return "MEDIUM"


def event_type_ru(event_type: str | None) -> str:
    if not event_type or not str(event_type).strip():
        return "Корпоративное событие"
    raw = str(event_type).strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")
    if key in EVENT_TYPE_RU:
        return EVENT_TYPE_RU[key]
    if re.fullmatch(r"[a-z0-9_./ -]+", raw.lower()):
        return "Корпоративное событие"  # латинский код без перевода
    return raw  # уже человекочитаемое русское описание из разрешённого перечня


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def format_date_ru(value) -> str | None:
    """"2026-07-14" → "14 июля 2026". Пустые значения → None, нераспознанные → как есть."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "неизвестно"):
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return s
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not 1 <= mo <= 12:
        return s
    return f"{d} {MONTHS_RU[mo - 1]} {y}"


def format_datetime_msk(value=None) -> str:
    """UTC-метка из SQLite ("YYYY-MM-DD HH:MM:SS") → "14 июля 2026, 13:05 МСК"."""
    if value is None:
        dt = datetime.now(MSK)
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(MSK)
    return f"{dt.day} {MONTHS_RU[dt.month - 1]} {dt.year}, {dt:%H:%M} МСК"


def _time_msk(value) -> str:
    try:
        dt = datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc).astimezone(MSK)
        return f"{dt:%H:%M} МСК"
    except (ValueError, TypeError):
        return str(value)


def _loads_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _key_parameters(e: dict) -> list[dict]:
    params = _loads_list(e.get("key_parameters") or e.get("key_parameters_json"))
    result = [p for p in params if isinstance(p, dict) and p.get("label") and p.get("value") is not None]
    labels = {str(p["label"]).lower() for p in result}
    effective = format_date_ru(e.get("effective_date"))
    if effective and not any("вступлени" in l for l in labels):
        result.append({"label": "Дата вступления в силу", "value": effective})
    return result


def _sources(e: dict) -> list[dict]:
    result = []
    seen = set()
    if e.get("source_url"):
        result.append({"name": e.get("source_name") or "Официальный источник", "url": e["source_url"]})
        seen.add(e["source_url"])
    extra = _loads_list(e.get("additional_sources") or e.get("additional_sources_json"))
    extra += _loads_list(e.get("sources"))
    for s in extra:
        if isinstance(s, dict) and s.get("url") and s["url"] not in seen:
            seen.add(s["url"])
            result.append({"name": s.get("name") or "Дополнительное подтверждение", "url": s["url"]})
    return result


def _underlying_ticker(e: dict, asset: dict | None) -> str | None:
    ticker = (e.get("underlying_ticker") or "").strip()
    if ticker:
        return ticker
    for alias in (asset or {}).get("aliases", []):
        if len(alias) <= 12:
            return alias
    return None


def _shorten(text: str, limit: int = 350) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sentences:
        if out and len(out) + len(s) + 1 > limit:
            break
        out = f"{out} {s}".strip()
    return out or text[:limit].rsplit(" ", 1)[0] + "…"


def _recommended_action_line(e: dict) -> str | None:
    action = (e.get("recommended_action") or "").strip()
    required = e.get("action_required")
    if not action and required is None:
        return None
    if required is None:
        emoji = "👀"
    else:
        emoji = "⚠️" if required else "✅"
    if not action:
        action = ("Проверить необходимость корректировки параметров контракта."
                  if required else "Дополнительные действия не требуются.")
    return f"{emoji} {esc(action)}"


def format_event_full(e: dict, asset: dict | None = None) -> str:
    """Полный аналитический отчёт о событии (HTML). Пустые поля не выводятся."""
    severity = classify_severity(e.get("event_type"), e.get("severity"))
    parts: list[str] = [f"🚨 <b>{esc(event_type_ru(e.get('event_type')).upper())}</b>"]

    header: list[str] = []
    asset_name = (asset or {}).get("name") or e.get("asset_code")
    if asset_name:
        header.append(f"<b>Базовый актив:</b> {esc(asset_name)}")
    code = (asset or {}).get("code") or e.get("asset_code")
    if code:
        header.append(f"<b>Код инструмента:</b> <code>{esc(code)}</code>")
    ticker = _underlying_ticker(e, asset)
    if ticker:
        header.append(f"<b>Тикер базового актива:</b> <code>{esc(ticker)}</code>")
    event_date = format_date_ru(e.get("event_date"))
    if event_date:
        header.append(f"<b>Дата события:</b> {esc(event_date)}")
    pub_date = format_date_ru(e.get("publication_date"))
    if pub_date:
        header.append(f"<b>Дата публикации:</b> {esc(pub_date)}")
    status = STATUS_RU.get((e.get("status") or "").strip().lower())
    if status:
        header.append(f"<b>Статус:</b> {esc(status)}")
    header.append(f"<b>Важность:</b> {SEVERITY_LABEL[severity]}")
    parts.append("\n".join(header))

    summary = (e.get("summary_ru") or "").strip()
    if summary:
        parts.append(f"<b>Что произошло</b>\n{esc(summary)}")
    elif e.get("title"):
        parts.append(f"<b>Что произошло</b>\n{esc(e['title'])}")

    params = _key_parameters(e)
    if params:
        lines = "\n".join(f"• {esc(p['label'])}: {esc(p['value'])}" for p in params)
        parts.append(f"<b>Ключевые параметры</b>\n{lines}")

    impact = (e.get("impact_ru") or "").strip()
    if impact:
        parts.append(f"<b>Возможное влияние</b>\n{esc(impact)}")

    action_line = _recommended_action_line(e)
    if action_line:
        parts.append(f"<b>Рекомендуемое действие</b>\n{action_line}")

    sources = _sources(e)
    if sources:
        lines = "\n".join(
            f"{i}. <a href=\"{esc(s['url'])}\">{esc(s['name'])}</a>" for i, s in enumerate(sources, 1))
        parts.append(f"<b>Источники</b>\n{lines}")

    footer = []
    confidence = float(e.get("confidence") or 0)
    if confidence:
        footer.append(f"<b>Достоверность:</b> {round(confidence * 100)}%")
    footer.append(f"<b>Обнаружено:</b> {esc(format_datetime_msk(e.get('detected_at')))}")
    parts.append("\n".join(footer))

    return "\n\n".join(parts)


def format_event_short(e: dict, asset: dict | None = None, include_event_command: bool = True) -> str:
    """Краткое уведомление: помещается в одно сообщение."""
    severity = classify_severity(e.get("event_type"), e.get("severity"))
    lines = [f"🚨 <b>{esc(event_type_ru(e.get('event_type')))}</b>"]
    code = (asset or {}).get("code") or e.get("asset_code", "")
    name = (asset or {}).get("name")
    lines.append(f"<code>{esc(code)}</code>" + (f" · {esc(name)}" if name else ""))
    lines.append(SEVERITY_LABEL[severity])

    body = _shorten(e.get("summary_ru") or e.get("title") or "")
    blocks = ["\n".join(lines)]
    if body:
        blocks.append(esc(body))
    action_line = _recommended_action_line(e)
    if action_line:
        blocks.append(action_line)

    tail = []
    sources = _sources(e)
    if sources:
        tail.append(f"<a href=\"{esc(sources[0]['url'])}\">{esc(sources[0]['name'])}</a>")
    if include_event_command and e.get("id"):
        tail.append(f"Подробнее: /event {e['id']}")
    if tail:
        blocks.append("\n".join(tail))
    return "\n\n".join(blocks)


def event_details_button(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Подробнее", callback_data=f"event:{event_id}")]])


def format_scan_summary(stats, started_at: datetime | None = None,
                        finished_at: datetime | None = None,
                        new_events: list[dict] | None = None) -> str:
    """Итог проверки для /scan: полная воронка всегда, судьба каждого этапа объяснена."""
    lines = ["✅ <b>Проверка завершена</b>", ""]
    if started_at and finished_at:
        lines.append(f"<b>Период:</b> {started_at.astimezone(MSK):%H:%M}–{finished_at.astimezone(MSK):%H:%M} МСК")
    lines += [
        f"• проверено источников: {stats.sources_checked}",
        f"• скачано публикаций: {stats.publications}",
        f"  ├ RSS: {stats.rss_publications}",
        f"  ├ HTML: {stats.html_publications}",
        f"  └ Tavily: {stats.tavily_publications}",
        f"• отсечено кэшем: {stats.cache_hits}",
        f"• новых после кэша: {stats.new_after_cache}",
        f"• прошло keyword-фильтр: {stats.after_keywords}",
        "• сопоставлено:",
        f"  ├ high: {stats.matched_high}",
        f"  ├ medium: {stats.matched_medium}",
        f"  ├ low: {stats.matched_low}",
        f"  └ unmatched: {stats.unmatched}",
        f"• нерелевантных из глобальных фидов: {stats.irrelevant_global}",
        f"• передано в Gemini: {stats.after_filter}",
        f"• вызовов Gemini: {stats.llm_calls}",
        f"• событий от модели: {stats.llm_events}",
        f"• новых событий: {stats.saved}",
        f"• дубликатов событий: {stats.duplicates}",
        f"• ошибок: {stats.errors}",
        f"• стоимость: ${stats.cost_usd:.5f}",
    ]
    if stats.after_keywords and not stats.after_filter:
        lines += ["", f"ℹ️ {stats.after_keywords} публикаций прошли фильтр, но ни одна не достигла "
                      "уровня high/medium для передачи в Gemini."]
    if stats.llm_failed:
        lines += ["", f"⚠️ Сбой Gemini: {stats.llm_failed} публикаций не проанализированы "
                      "и будут повторены при следующем скане."]
    if not stats.saved:
        lines += ["", "Новых событий не обнаружено."]
    if new_events:
        lines += ["", "<b>Новые события</b>"]
        for e in new_events:
            severity = classify_severity(e.get("event_type"), e.get("severity"))
            type_ru = event_type_ru(e.get("event_type"))
            type_ru = type_ru[:1].lower() + type_ru[1:]
            lines.append(f"{SEVERITY_EMOJI[severity]} {esc(e.get('asset_code', ''))} — {esc(type_ru)}")
    return "\n".join(lines)


def _run_duration(run: dict) -> str:
    try:
        start = datetime.fromisoformat(str(run["started_at"]))
        end = datetime.fromisoformat(str(run["finished_at"]))
        return f"{int((end - start).total_seconds())} c"
    except (TypeError, ValueError):
        return "—"


def format_scan_history(runs: list[dict]) -> str:
    """История проверок для /scans."""
    if not runs:
        return "Проверок ещё не было."
    blocks = ["🕓 <b>История проверок</b> (последние " + str(len(runs)) + ")"]
    for run in runs:
        stats = {}
        try:
            stats = json.loads(run.get("stats_json") or "{}")
        except json.JSONDecodeError:
            pass
        status_emoji = "✅" if run.get("status") == "ok" else ("⏳" if not run.get("finished_at") else "❌")
        lines = [f"{status_emoji} #{run['id']} · {esc(format_datetime_msk(run['started_at']))} · {_run_duration(run)}"]
        if stats:
            lines.append(
                f"   публикаций {stats.get('publications', 0)} · кэш {stats.get('cache_hits', 0)} · "
                f"в LLM {stats.get('after_filter', 0)} · событий {stats.get('saved', run.get('new_events', 0))} · "
                f"${stats.get('cost_usd', 0):.4f}")
        else:
            lines.append(f"   событий {run.get('new_events', 0)}")
        if run.get("error"):
            lines.append(f"   ошибка: {esc(str(run['error'])[:120])}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_ARTICLE_STATUS_RU = {
    "keyword_filtered": "⚪ без признаков события",
    "unmatched": "🔵 не сопоставлена с активом",
    "irrelevant_global": "⚪ чужая (глобальный фид)",
    "llm_analyzed": "🟡 LLM: не событие/отклонено",
    "rejected": "🟡 LLM: отклонено",
    "event_created": "🟢 создано событие",
    "matched": "📥 в очереди на анализ",
    "rematch_high": "📥 в очереди (rematch)",
    "rematch_medium": "📥 в очереди (rematch)",
    "ignored": "🚫 помечена нерелевантной",
    "fetched": "…получена, ждёт обработки",
}


def format_articles(rows: list[dict]) -> str:
    """Последние обработанные публикации для /articles."""
    if not rows:
        return "Обработанных публикаций пока нет."
    blocks = ["📰 <b>Последние публикации в обработке</b>"]
    for r in rows:
        status = _ARTICLE_STATUS_RU.get(r.get("status") or "", r.get("status") or "—")
        line1 = f"• {esc((r.get('title') or r.get('url') or '')[:90])}"
        details = [esc(r.get("source") or "—"), status]
        if r.get("best_asset"):
            details.append(f"кандидат: {esc(r['best_asset'])} ({(r.get('best_score') or 0):.2f})")
        blocks.append(line1 + "\n  " + " · ".join(details))
    return "\n".join(blocks)


def format_cost_report(costs: dict, model: str) -> str:
    """Отчёт /cost: стоимость сегодня и за месяц, запросы, экономия кэша."""
    def block(title: str, c: dict) -> str:
        return (f"<b>{title}</b>\n"
                f"• запросов к модели: {c['calls']}\n"
                f"• токены: {c['input_tokens']:,} вход / {c['output_tokens']:,} выход\n"
                f"• стоимость: ${c['cost_usd']:.4f}\n"
                f"• публикаций отсечено кэшем: {c['cache_hits']}").replace(",", " ")
    # Экономия: кэш-попадания не дошли до модели; оцениваем по средней цене публикации.
    total = costs["total"]
    processed = max(1, total["calls"] * 20)  # ~20 публикаций на вызов
    avg_per_pub = total["cost_usd"] / processed if total["cost_usd"] else 0.0002
    saved_usd = total["cache_hits"] * avg_per_pub
    return ("💰 <b>Стоимость LLM</b>\n"
            f"Модель: <code>{esc(model)}</code>\n\n"
            + block("Сегодня", costs["today"]) + "\n\n"
            + block("Текущий месяц", costs["month"]) + "\n\n"
            + f"<b>Экономия за счёт кэша (всего):</b> ≈${saved_usd:.4f}")


def format_digest(events: list[dict], last_run: dict | None = None) -> str:
    """Сводка событий за последние 24 часа для /digest."""
    if not events:
        lines = ["🗂 <b>Сводка событий</b>", "",
                 "За последние 24 часа новых событий не обнаружено."]
        if last_run:
            lines.append("")
            lines.append(f"Последняя успешная проверка: {esc(format_datetime_msk(last_run.get('finished_at') or last_run.get('started_at')))}")
            if last_run.get("assets_scanned") is not None:
                lines.append(f"Проверено активов: {last_run['assets_scanned']}")
            if last_run.get("errors") is not None:
                lines.append(f"Ошибок: {last_run['errors']}")
        return "\n".join(lines)

    counts: dict[str, int] = {}
    for e in events:
        severity = classify_severity(e.get("event_type"), e.get("severity"))
        counts[severity] = counts.get(severity, 0) + 1
    severity_names = {"CRITICAL": "Критические", "HIGH": "Высокие", "MEDIUM": "Средние", "LOW": "Низкие"}
    lines = ["🗂 <b>Сводка событий</b>", "Период: последние 24 часа", ""]
    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        if counts.get(severity):
            lines.append(f"{SEVERITY_EMOJI[severity]} {severity_names[severity]} — {counts[severity]}")
    for i, e in enumerate(events, 1):
        type_ru = event_type_ru(e.get("event_type"))
        type_ru = type_ru[:1].lower() + type_ru[1:]
        item = [f"{i}. <b>{esc(e.get('asset_code', ''))}</b> — {esc(type_ru)}"]
        summary = _shorten(e.get("summary_ru") or e.get("title") or "", 200)
        if summary:
            item.append(esc(summary))
        if e.get("detected_at"):
            item.append(f"Обнаружено: {esc(_time_msk(e['detected_at']))}")
        if e.get("id"):
            item.append(f"/event {e['id']}")
        lines += ["", "\n".join(item)]
    return "\n".join(lines)


def format_events_list(events: list[dict]) -> str:
    """Компактная история событий для /events."""
    if not events:
        return "История событий пуста."
    blocks = []
    for e in events:
        severity = classify_severity(e.get("event_type"), e.get("severity"))
        lines = []
        head = f"<b>#{e['id']} · {esc(e.get('asset_code', ''))}</b>" if e.get("id") else f"<b>{esc(e.get('asset_code', ''))}</b>"
        lines.append(head)
        lines.append(f"{SEVERITY_EMOJI[severity]} {esc(event_type_ru(e.get('event_type')))}")
        event_date = format_date_ru(e.get("event_date"))
        if event_date:
            lines.append(esc(event_date))
        one_liner = _shorten(e.get("title") or e.get("summary_ru") or "", 100)
        if one_liner:
            lines.append(esc(one_liner))
        if e.get("id"):
            lines.append(f"/event {e['id']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def split_html(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Режет длинный HTML-текст на части, не разрывая строки (а значит — теги и ссылки:
    форматтеры этого модуля не переносят теги через границу строки)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
            continue
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                while len(line) > limit:  # крайний случай: одна строка длиннее лимита
                    cut = line.rfind(" ", 0, limit)
                    cut = cut if cut > 0 else limit
                    chunks.append(line[:cut])
                    line = line[cut:].strip()
                current = line
    if current:
        chunks.append(current)
    return chunks


def strip_html(text: str) -> str:
    """Fallback: превращает HTML-сообщение в обычный текст."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))
