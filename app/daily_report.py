"""Дневной Excel-отчёт: построчно всё, что мониторинг проверял по базовым активам.

Четыре листа:
- «Публикации» — каждая публикация с кандидатом-активом: заголовок (оригинал и
  перевод на русский), источник, ссылка, этап конвейера, score сопоставления,
  предполагаемый тип события, вердикт Gemini и краткое резюме (включая
  отклонённые — для контроля фильтров). Сортировка — по score: чем выше, тем
  увереннее публикация относится к активу.
- «События дня» — созданные события с датами и параметрами.
- «Сводка по активам» — все отслеживаемые активы: сколько публикаций прошло
  по каждому и до какого этапа; нули видны явно (пробелы покрытия).
- «Источники» — каждая лента: подписчики, когда проверена, ошибки, сколько
  публикаций с неё обработано за сутки (в том числе отсеянных фильтром ключевых
  слов) и сколько получили кандидата-актива.

Перевод заголовков делает Gemini одним-двумя батч-запросами перед сборкой
файла (translate_titles); результат сохраняется в processed_articles.title_ru,
поэтому повторный /summary ничего не переводит заново. При сбое модели
колонка остаётся пустой — отчёт всё равно уходит.

openpyxl импортируется лениво: отчёт строится раз в день, держать библиотеку
в памяти постоянно незачем.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from io import BytesIO

from app import db
from app.formatters import MSK, event_type_ru
from app.matcher import url_hash

log = logging.getLogger(__name__)

STAGE_RU = {
    "event_created": "Событие создано",
    "rejected": "Gemini: отклонено",
    "llm_analyzed": "Gemini: не событие",
    "matched": "Сопоставлено, ждёт Gemini",
    "rematch_high": "Пересопоставлено (high)",
    "rematch_medium": "Пересопоставлено (medium)",
    "rematch_low": "Пересопоставлено (low)",
    "rematch_unmatched": "Пересопоставление: нет кандидата",
    "unmatched": "Слабый кандидат, в Gemini не передано",
    "irrelevant_global": "Нерелевантно (глобальный источник)",
    "keyword_filtered": "Отсечено фильтром ключевых слов",
    "ignored": "Проигнорировано вручную",
    "fetched": "Скачано, не обработано",
}
# Порядок этапов — от финального к раннему: при равном score финальные выше.
_STAGE_ORDER = list(STAGE_RU)

INSTRUMENT_RU = {"SHARE": "Акция", "STOCK": "Акция", "ADR": "АДР", "ETF": "ETF",
                 "FUND": "Фонд", "INDEX": "Индекс", "CRYPTO": "Криптоактив",
                 "ASSET": "Товар"}
SOURCE_TYPE_RU = {"RSS": "RSS", "SEC": "SEC EDGAR", "IR": "Сайт эмитента/фонда",
                  "EXCHANGE": "Биржа", "NEWS": "Google News", "HTML": "HTML-страница",
                  "SEARCH": "Поиск (Tavily)"}

PUBLICATION_HEADERS = ["№", "Код актива", "Актив", "Тип инструмента", "Заголовок",
                       "Заголовок (рус.)", "Источник", "Ссылка", "Опубликовано",
                       "Проверено (МСК)", "Этап", "Score", "Предполагаемый тип события",
                       "Вердикт Gemini", "Резюме Gemini"]
EVENT_HEADERS = ["Код актива", "Тип события", "Важность", "Заголовок", "Заголовок (рус.)",
                 "Дата события", "Вступает в силу", "Резюме", "Влияние", "Рекомендация",
                 "Уверенность", "Источник", "Ссылка", "Создано (МСК)"]
ASSET_HEADERS = ["Код актива", "Актив", "Тип инструмента", "Публикаций всего",
                 "Передано в Gemini", "Отклонено Gemini", "Событий создано", "Слабые кандидаты"]
SOURCE_HEADERS = ["Источник", "Тип", "Ссылка", "Подписчики", "Включён", "Проверен (МСК)",
                  "Публикаций за период", "С кандидатом-активом", "Событий", "Ошибок подряд",
                  "Последняя ошибка"]

TRANSLATE_BATCH = 60
TRANSLATE_PROMPT = (
    "Переведи заголовки финансовых новостей на русский язык. Перевод точный и естественный, "
    "деловой стиль; названия компаний, фондов, бирж, тикеры и коды форм (8-K, 6-K) не переводи. "
    "Верни только JSON-массив объектов вида {\"i\": номер, \"ru\": \"перевод\"} — по одному "
    "на каждый заголовок, без пояснений.\n\nЗаголовки:\n")


def _msk(value) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:19]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def _published(value) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return str(value)[:16]


def _verdict(row: dict) -> str:
    """Человекочитаемый вердикт из llm_result/status."""
    status, result = row.get("status") or "", row.get("llm_result") or ""
    if status == "event_created":
        return "Событие: " + event_type_ru(result)
    if result.startswith("rejected:"):
        return "Отклонено: " + result[len("rejected:"):].strip()
    if status == "llm_analyzed":
        return "Не событие"
    if result.startswith("skipped:no_asset_match"):
        return "Нет уверенного совпадения с активом"
    if result.startswith("skipped:irrelevant_global"):
        return "Публикация глобального источника не об отслеживаемых активах"
    if result.startswith("skipped:routine_sec_form"):
        return "Рутинная форма SEC"
    if result.startswith("skipped:no_keywords"):
        return "Нет ключевых слов корпоративного события"
    if result.startswith("skipped:"):
        return result[len("skipped:"):]
    if row.get("pending_llm"):
        return "В очереди на анализ"
    return result or ""


def _proposed_type(row: dict) -> str:
    t = row.get("llm_event_type")
    if not t or t.lower() in ("none", "null", "other", "not_event"):
        return ""
    return event_type_ru(t)


def stage_ru(status: str | None) -> str:
    return STAGE_RU.get(status or "fetched", status or "")


def instrument_ru(instrument_type: str | None) -> str:
    return INSTRUMENT_RU.get((instrument_type or "").upper(), instrument_type or "")


def _source_label(row: dict) -> str:
    """Имя источника для строки публикации. У резервных Google News-лент имя одно
    на всех («news.google.com») — показываем, что это лента актива."""
    name = row.get("source") or ""
    if (row.get("source_type") == "NEWS" or "news.google.com" in (row.get("source_url") or "")) \
            and row.get("best_asset"):
        return f"Google News ({row['best_asset']})"
    return name


# --- Перевод заголовков --------------------------------------------------------

def _parse_translations(text: str) -> dict[int, str]:
    from app.llm import _parse_json_array
    out: dict[int, str] = {}
    for item in _parse_json_array(text):
        try:
            idx, ru = int(item.get("i")), str(item.get("ru") or "").strip()
        except (TypeError, ValueError):
            continue
        if ru:
            out[idx] = ru
    return out


async def translate_titles(provider, hours: int = 24) -> dict:
    """Переводит заголовки публикаций периода, у которых ещё нет title_ru.
    Возвращает счётчики {"requested", "translated", "calls", "input_tokens", "output_tokens", "cost_usd"}.
    Ошибка одного батча не останавливает остальные."""
    pending = db.untranslated_titles(hours)
    counts = {"requested": len(pending), "translated": 0, "calls": 0,
              "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    if not pending:
        return counts
    for start in range(0, len(pending), TRANSLATE_BATCH):
        batch = pending[start:start + TRANSLATE_BATCH]
        prompt = TRANSLATE_PROMPT + "\n".join(f"{i}. {title[:300]}" for i, (_, title) in enumerate(batch))
        try:
            text, usage = await provider.generate(prompt, max_output_tokens=8192)
        except Exception as exc:
            log.warning("Перевод заголовков (батч %d–%d) не удался: %s: %s",
                        start, start + len(batch), type(exc).__name__, exc)
            continue
        counts["calls"] += usage.calls
        counts["input_tokens"] += usage.input_tokens
        counts["output_tokens"] += usage.output_tokens
        counts["cost_usd"] += usage.cost_usd
        parsed = _parse_translations(text)
        saved = db.set_title_ru({batch[i][0]: ru for i, ru in parsed.items() if 0 <= i < len(batch)})
        counts["translated"] += saved
        if saved < len(batch):
            log.info("Перевод заголовков: батч %d–%d — переведено %d из %d",
                     start, start + len(batch), saved, len(batch))
    return counts


# --- Сводка по активам ---------------------------------------------------------

def build_asset_summary(rows: list[dict], events: list[dict],
                        assets: list[dict] | None = None) -> list[dict]:
    """Агрегат по активам: публикации, дошедшие до Gemini, отклонённые, события, слабые.
    Все переданные активы попадают в сводку, даже с нулями — так видно, по каким
    классам инструментов публикаций не было вовсе."""
    by_asset: dict[str, dict] = {}

    def entry(code: str, name: str = "", itype: str = "") -> dict:
        return by_asset.setdefault(code, {"code": code, "name": name, "type": itype,
                                          "total": 0, "llm": 0, "rejected": 0,
                                          "events": 0, "weak": 0})

    for a in assets or []:
        entry(a["code"], a.get("name") or "", a.get("instrument_type") or "")
    for r in rows:
        a = entry(r["best_asset"], r.get("asset_name") or "", r.get("instrument_type") or "")
        a["total"] += 1
        if r["status"] in ("llm_analyzed", "rejected", "event_created"):
            a["llm"] += 1
        if r["status"] == "rejected":
            a["rejected"] += 1
        if r["status"] in ("unmatched", "rematch_low", "rematch_unmatched"):
            a["weak"] += 1
    for e in events:
        entry(e.get("asset_code"))["events"] += 1
    return sorted(by_asset.values(), key=lambda a: (-a["events"], -a["llm"], -a["total"], a["code"]))


def build_workbook(hours: int = 24) -> tuple[bytes, dict]:
    """Строит xlsx; возвращает байты и счётчики для подписи к файлу."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = db.daily_report_rows(hours)
    events = db.events_since_hours(hours)
    assets = db.list_assets(enabled_only=True)
    summary = build_asset_summary(rows, events, assets)
    sources = db.source_report_rows(hours)
    # Важнее — выше: score, затем этап (финальные раньше), затем время проверки.
    rows.sort(key=lambda r: (-(r.get("best_score") or 0.0),
                             _STAGE_ORDER.index(r["status"]) if r["status"] in _STAGE_ORDER else 99,
                             r.get("processed_at") or ""))
    # Перевод для события ищется по ссылке публикации-источника (хэш считается от URL).
    title_ru_by_hash = {url_hash(r.get("url") or ""): r.get("title_ru") or "" for r in rows
                        if r.get("title_ru")}
    active_assets = sum(1 for a in summary if a["total"] or a["events"])

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F4E78")
    link_font = Font(color="0563C1", underline="single")
    wrap = Alignment(wrap_text=True, vertical="top")
    stage_fill = {
        "event_created": PatternFill("solid", fgColor="C6EFCE"),
        "rejected": PatternFill("solid", fgColor="FFEB9C"),
        "llm_analyzed": PatternFill("solid", fgColor="F2F2F2"),
    }
    error_fill = PatternFill("solid", fgColor="FFC7CE")
    zero_fill = PatternFill("solid", fgColor="F2F2F2")

    def header(ws, headers, widths):
        ws.append(headers)
        for i, (cell, w) in enumerate(zip(ws[1], widths), start=1):
            cell.font, cell.fill, cell.alignment = head_font, head_fill, Alignment(vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    def link(cell, url):
        if url:
            cell.hyperlink = url
            cell.font = link_font

    wb = Workbook()
    ws = wb.active
    ws.title = "Публикации"
    header(ws, PUBLICATION_HEADERS, [5, 11, 28, 13, 50, 50, 24, 40, 12, 16, 30, 7, 26, 38, 60])
    for n, r in enumerate(rows, start=1):
        ws.append([n, r["best_asset"], r.get("asset_name") or "", instrument_ru(r.get("instrument_type")),
                   (r.get("title") or "")[:500], (r.get("title_ru") or "")[:500],
                   _source_label(r), r.get("url") or "",
                   _published(r.get("published_at")), _msk(r.get("processed_at")),
                   stage_ru(r["status"]),
                   round(r["best_score"], 2) if r.get("best_score") is not None else None,
                   _proposed_type(r), _verdict(r), (r.get("llm_summary") or "")[:600]])
        row_cells = ws[ws.max_row]
        link(row_cells[7], r.get("url"))
        for c in (row_cells[4], row_cells[5], row_cells[13], row_cells[14]):
            c.alignment = wrap
        if r["status"] in stage_fill:
            row_cells[10].fill = stage_fill[r["status"]]

    ws2 = wb.create_sheet("События дня")
    header(ws2, EVENT_HEADERS, [11, 26, 10, 45, 45, 12, 12, 60, 45, 40, 10, 22, 40, 16])
    for e in events:
        try:
            params = json.loads(e.get("key_parameters_json") or "[]")
        except ValueError:
            params = []
        summary_text = e.get("summary_ru") or ""
        if params:
            summary_text += "\n" + "; ".join(f"{p.get('label')}: {p.get('value')}" for p in params
                                             if isinstance(p, dict))
        ws2.append([e.get("asset_code"), event_type_ru(e.get("event_type")), e.get("severity") or "",
                    e.get("title") or "", title_ru_by_hash.get(url_hash(e.get("source_url") or ""), ""),
                    e.get("event_date") or "", e.get("effective_date") or "",
                    summary_text, e.get("impact_ru") or "", e.get("recommended_action") or "",
                    round(float(e.get("confidence") or 0), 2), e.get("source_name") or "",
                    e.get("source_url") or "", _msk(e.get("detected_at"))])
        cells = ws2[ws2.max_row]
        link(cells[12], e.get("source_url"))
        for c in (cells[3], cells[4], cells[7], cells[8], cells[9]):
            c.alignment = wrap

    ws3 = wb.create_sheet("Сводка по активам")
    header(ws3, ASSET_HEADERS, [11, 34, 14, 16, 18, 16, 16, 16])
    for a in summary:
        ws3.append([a["code"], a["name"], instrument_ru(a["type"]), a["total"], a["llm"],
                    a["rejected"], a["events"], a["weak"]])
        if not a["total"] and not a["events"]:
            for c in ws3[ws3.max_row]:
                c.fill = zero_fill
    ws3.append([])
    ws3.append(["Итого", f"{active_assets} из {len(summary)} активов с публикациями", "", len(rows),
                sum(a["llm"] for a in summary), sum(a["rejected"] for a in summary),
                len(events), sum(a["weak"] for a in summary)])
    for c in ws3[ws3.max_row]:
        c.font = Font(bold=True)

    ws4 = wb.create_sheet("Источники")
    header(ws4, SOURCE_HEADERS, [26, 18, 60, 22, 9, 16, 12, 12, 9, 10, 50])
    for s in sources:
        ws4.append([s.get("name") or "", SOURCE_TYPE_RU.get(s.get("type") or "", s.get("type") or ""),
                    s.get("url") or "", s.get("subscribers") or "", "да" if s.get("enabled") else "нет",
                    _msk(s.get("last_checked_at")), s.get("fetched") or 0, s.get("matched") or 0,
                    s.get("events") or 0, s.get("fail_streak") or 0, (s.get("last_error") or "")[:300]])
        cells = ws4[ws4.max_row]
        link(cells[2], s.get("url"))
        cells[10].alignment = wrap
        if s.get("last_error") or not s.get("enabled"):
            cells[0].fill = error_fill
    ws4.append([])
    ws4.append(["Итого", f"{len(sources)} лент", "", "",
                f"{sum(1 for s in sources if s.get('enabled'))} вкл.", "",
                sum(s.get("fetched") or 0 for s in sources), sum(s.get("matched") or 0 for s in sources),
                sum(s.get("events") or 0 for s in sources),
                f"{sum(1 for s in sources if s.get('last_error'))} с ошибкой", ""])
    for c in ws4[ws4.max_row]:
        c.font = Font(bold=True)

    buf = BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue(), {"publications": len(rows), "events": len(events),
                            "assets": active_assets, "assets_total": len(summary),
                            "llm": sum(a["llm"] for a in summary),
                            "sources": len(sources),
                            "sources_failing": sum(1 for s in sources if s.get("last_error")),
                            "fetched": sum(s.get("fetched") or 0 for s in sources)}


def report_filename(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    return f"monitoring_{now:%Y-%m-%d}.xlsx"


def caption(counts: dict) -> str:
    text = (f"📎 Детализация дня: {counts['publications']} публикаций по {counts['assets']} "
            f"из {counts.get('assets_total', counts['assets'])} активам, "
            f"до Gemini дошло {counts['llm']}, событий — {counts['events']}.")
    if counts.get("sources"):
        text += (f" Источников: {counts['sources']}, с ошибкой — {counts.get('sources_failing', 0)}, "
                 f"скачано публикаций — {counts.get('fetched', 0)}.")
    return text
