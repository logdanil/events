"""Дневной Excel-отчёт: построчно всё, что мониторинг проверял по базовым активам.

Три листа:
- «Публикации» — каждая публикация с кандидатом-активом: заголовок, источник,
  ссылка, этап конвейера, score сопоставления, предполагаемый тип события,
  вердикт Gemini и краткое резюме (включая отклонённые — для контроля фильтров).
- «События дня» — созданные события с датами и параметрами.
- «Сводка по активам» — сколько публикаций прошло по каждому активу и до какого этапа.

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
# Порядок этапов — от финального к раннему: так сводка по активу читается сверху вниз.
_STAGE_ORDER = list(STAGE_RU)

PUBLICATION_HEADERS = ["№", "Код актива", "Актив", "Тип инструмента", "Заголовок",
                       "Источник", "Ссылка", "Опубликовано", "Проверено (МСК)", "Этап",
                       "Score", "Предполагаемый тип события", "Вердикт Gemini", "Резюме Gemini"]
EVENT_HEADERS = ["Код актива", "Тип события", "Важность", "Заголовок", "Дата события",
                 "Вступает в силу", "Резюме", "Влияние", "Рекомендация", "Уверенность",
                 "Источник", "Ссылка", "Создано (МСК)"]
ASSET_HEADERS = ["Код актива", "Актив", "Публикаций всего", "Передано в Gemini",
                 "Отклонено Gemini", "Событий создано", "Слабые кандидаты"]


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


def build_asset_summary(rows: list[dict], events: list[dict]) -> list[dict]:
    """Агрегат по активам: публикации, дошедшие до Gemini, отклонённые, события, слабые."""
    by_asset: dict[str, dict] = {}
    for r in rows:
        code = r["best_asset"]
        a = by_asset.setdefault(code, {"code": code, "name": r.get("asset_name") or "",
                                       "total": 0, "llm": 0, "rejected": 0,
                                       "events": 0, "weak": 0})
        a["total"] += 1
        if r["status"] in ("llm_analyzed", "rejected", "event_created"):
            a["llm"] += 1
        if r["status"] == "rejected":
            a["rejected"] += 1
        if r["status"] in ("unmatched", "rematch_low", "rematch_unmatched"):
            a["weak"] += 1
    for e in events:
        code = e.get("asset_code")
        a = by_asset.setdefault(code, {"code": code, "name": "", "total": 0, "llm": 0,
                                       "rejected": 0, "events": 0, "weak": 0})
        a["events"] += 1
    return sorted(by_asset.values(), key=lambda a: (-a["events"], -a["llm"], -a["total"], a["code"]))


def build_workbook(hours: int = 24) -> tuple[bytes, dict]:
    """Строит xlsx; возвращает байты и счётчики для подписи к файлу."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = db.daily_report_rows(hours)
    events = db.events_since_hours(hours)
    summary = build_asset_summary(rows, events)
    rows.sort(key=lambda r: (_STAGE_ORDER.index(r["status"]) if r["status"] in _STAGE_ORDER else 99,
                             r["best_asset"], r.get("processed_at") or ""), )

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F4E78")
    link_font = Font(color="0563C1", underline="single")
    wrap = Alignment(wrap_text=True, vertical="top")
    stage_fill = {
        "event_created": PatternFill("solid", fgColor="C6EFCE"),
        "rejected": PatternFill("solid", fgColor="FFEB9C"),
        "llm_analyzed": PatternFill("solid", fgColor="F2F2F2"),
    }

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
    header(ws, PUBLICATION_HEADERS, [5, 11, 28, 13, 60, 24, 40, 12, 16, 30, 7, 26, 38, 60])
    for n, r in enumerate(rows, start=1):
        ws.append([n, r["best_asset"], r.get("asset_name") or "", r.get("instrument_type") or "",
                   (r.get("title") or "")[:500], r.get("source") or "", r.get("url") or "",
                   _published(r.get("published_at")), _msk(r.get("processed_at")),
                   stage_ru(r["status"]),
                   round(r["best_score"], 2) if r.get("best_score") is not None else None,
                   _proposed_type(r), _verdict(r), (r.get("llm_summary") or "")[:600]])
        row_cells = ws[ws.max_row]
        link(row_cells[6], r.get("url"))
        for c in (row_cells[4], row_cells[12], row_cells[13]):
            c.alignment = wrap
        if r["status"] in stage_fill:
            row_cells[9].fill = stage_fill[r["status"]]

    ws2 = wb.create_sheet("События дня")
    header(ws2, EVENT_HEADERS, [11, 26, 10, 50, 12, 12, 60, 45, 40, 10, 22, 40, 16])
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
                    e.get("title") or "", e.get("event_date") or "", e.get("effective_date") or "",
                    summary_text, e.get("impact_ru") or "", e.get("recommended_action") or "",
                    round(float(e.get("confidence") or 0), 2), e.get("source_name") or "",
                    e.get("source_url") or "", _msk(e.get("detected_at"))])
        cells = ws2[ws2.max_row]
        link(cells[11], e.get("source_url"))
        for c in (cells[3], cells[6], cells[7], cells[8]):
            c.alignment = wrap

    ws3 = wb.create_sheet("Сводка по активам")
    header(ws3, ASSET_HEADERS, [11, 34, 16, 18, 16, 16, 16])
    for a in summary:
        ws3.append([a["code"], a["name"], a["total"], a["llm"], a["rejected"], a["events"], a["weak"]])
    ws3.append([])
    ws3.append(["Итого", f"{len(summary)} активов", len(rows),
                sum(a["llm"] for a in summary), sum(a["rejected"] for a in summary),
                len(events), sum(a["weak"] for a in summary)])
    for c in ws3[ws3.max_row]:
        c.font = Font(bold=True)

    buf = BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue(), {"publications": len(rows), "events": len(events),
                            "assets": len(summary),
                            "llm": sum(a["llm"] for a in summary)}


def report_filename(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    return f"monitoring_{now:%Y-%m-%d}.xlsx"


def caption(counts: dict) -> str:
    return (f"📎 Детализация дня: {counts['publications']} публикаций по {counts['assets']} активам, "
            f"до Gemini дошло {counts['llm']}, событий — {counts['events']}.")
