from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import telegram.error

from app import formatters
from app.bot import reply_html
from app.monitor import ScanStats

FULL_EVENT = {
    "id": 17,
    "asset_code": "SPYF",
    "event_type": "stock_split",
    "severity": "CRITICAL",
    "title": "Дробление паёв фонда 1:5",
    "summary_ru": "Эмитент объявил дробление паёв в соотношении 1:5. Количество паёв вырастет впятеро.",
    "impact_ru": "Цена пая снизится кратно коэффициенту, потребуется пересчёт параметров фьючерса.",
    "action_required": 1,
    "recommended_action": "Проверить необходимость корректировки параметров контракта.",
    "event_date": "2026-07-14",
    "publication_date": "2026-07-13",
    "effective_date": "2026-07-21",
    "status": "confirmed",
    "underlying_ticker": "SPY",
    "confidence": 0.94,
    "source_url": "https://example.com/official?a=1&b=2",
    "source_name": "SSGA Investor Relations",
    "key_parameters_json": '[{"label": "Коэффициент дробления", "value": "1:5"}]',
    "additional_sources_json": '[{"name": "Reuters", "url": "https://example.com/extra"}]',
    "detected_at": "2026-07-14 10:05:00",
}
ASSET = {"code": "SPYF", "name": "SPDR S&P 500 ETF Trust", "aliases": ["SPY"], "sources": []}


def test_html_escaping():
    e = {**FULL_EVENT, "title": None, "summary_ru": 'Условие: цена <5 & >3 "кавычки"', "asset_code": "A&B"}
    text = formatters.format_event_full(e)
    assert "&lt;5 &amp; &gt;3" in text
    assert "<5" not in text.replace("&lt;5", "")
    # Используются только разрешённые теги.
    assert not set(re.findall(r"</?([a-z]+)", text)) - {"b", "i", "code", "a"}


def test_full_event_contains_all_blocks():
    text = formatters.format_event_full(FULL_EVENT, ASSET)
    assert "🚨 <b>ДРОБЛЕНИЕ АКЦИЙ</b>" in text
    assert "<b>Базовый актив:</b> SPDR S&amp;P 500 ETF Trust" in text
    assert "<b>Код инструмента:</b> <code>SPYF</code>" in text
    assert "<b>Тикер базового актива:</b> <code>SPY</code>" in text
    assert "<b>Дата события:</b> 14 июля 2026" in text
    assert "<b>Дата публикации:</b> 13 июля 2026" in text
    assert "<b>Статус:</b> Подтверждено официальным источником" in text
    assert "<b>Важность:</b> 🔴 Критическая" in text
    assert "<b>Что произошло</b>" in text
    assert "• Коэффициент дробления: 1:5" in text
    assert "• Дата вступления в силу: 21 июля 2026" in text
    assert "<b>Возможное влияние</b>" in text
    assert "⚠️ Проверить необходимость корректировки параметров контракта." in text
    assert '1. <a href="https://example.com/official?a=1&amp;b=2">SSGA Investor Relations</a>' in text
    assert '2. <a href="https://example.com/extra">Reuters</a>' in text
    assert "<b>Достоверность:</b> 94%" in text
    assert "<b>Обнаружено:</b> 14 июля 2026, 13:05 МСК" in text


def test_minimal_event_hides_empty_blocks():
    e = {"asset_code": "GOLD", "event_type": "Тестовое событие", "title": "Заголовок",
         "summary_ru": "", "confidence": 0}
    text = formatters.format_event_full(e)
    assert "Дата события" not in text
    assert "Дата публикации" not in text
    assert "Тикер базового актива" not in text
    assert "Статус:" not in text
    assert "Ключевые параметры" not in text
    assert "Возможное влияние" not in text
    assert "Рекомендуемое действие" not in text
    assert "Источники" not in text
    assert "Достоверность" not in text
    assert "<b>Важность:</b>" in text
    assert "<b>Что произошло</b>\nЗаголовок" in text


def test_unknown_event_type():
    assert formatters.event_type_ru("some_unknown_code") == "Корпоративное событие"
    assert formatters.event_type_ru(None) == "Корпоративное событие"
    assert formatters.event_type_ru("stock_split") == "Дробление акций"
    # Русские описания из перечня методологии выводятся как есть.
    assert formatters.event_type_ru("Дробление паёв ETF (stock split)") == "Дробление паёв ETF (stock split)"


def test_severity_classification():
    assert formatters.classify_severity("Дробление паёв ETF (stock split)") == "CRITICAL"
    assert formatters.classify_severity("Делистинг ADR с биржи") == "CRITICAL"
    assert formatters.classify_severity("Изменение ADR-ratio (соотношения акций к одной ADR)") == "CRITICAL"
    assert formatters.classify_severity("Предложение о выкупе акций/ADR эмитентом (tender / issuer bid / buyback)") == "HIGH"
    assert formatters.classify_severity("Возврат капитала по ETF (capital distribution)") == "HIGH"
    assert formatters.classify_severity("Изменения маржинальных требований/ГО") == "MEDIUM"
    assert formatters.classify_severity("что-то совсем новое") == "MEDIUM"
    # Валидная важность от модели приоритетна.
    assert formatters.classify_severity("Изменения маржинальных требований/ГО", "LOW") == "LOW"
    assert formatters.classify_severity("x", "не важность") == "MEDIUM"


def test_short_event_fits_one_message():
    text = formatters.format_event_short(FULL_EVENT, ASSET)
    assert len(text) <= formatters.TELEGRAM_LIMIT
    assert "🚨 <b>Дробление акций</b>" in text
    assert "<code>SPYF</code> · SPDR S&amp;P 500 ETF Trust" in text
    assert "🔴 Критическая" in text
    assert "⚠️" in text
    assert '<a href="https://example.com/official?a=1&amp;b=2">SSGA Investor Relations</a>' in text
    assert "/event 17" in text


def test_split_long_message():
    long_block = "\n\n".join(f"<b>Блок {i}</b>\nстрока " + "x" * 200 for i in range(40))
    chunks = formatters.split_html(long_block)
    assert len(chunks) > 1
    assert all(len(c) <= formatters.TELEGRAM_LIMIT for c in chunks)
    # Теги не разорваны: в каждом куске <b> и </b> сбалансированы.
    for c in chunks:
        assert c.count("<b>") == c.count("</b>")
    assert "".join(chunks).replace("\n", "") == long_block.replace("\n", "")


def test_scan_summary_with_events():
    stats = ScanStats(sources_checked=42, publications=142, rss_publications=100,
                      html_publications=40, tavily_publications=2, cache_hits=30,
                      new_after_cache=112, after_keywords=12, matched_high=3, matched_medium=2,
                      matched_low=1, unmatched=4, irrelevant_global=2, after_filter=5,
                      llm_calls=1, llm_events=4, saved=2, duplicates=2, cost_usd=0.0042)
    events = [{"asset_code": "SPYF", "event_type": "stock_split"},
              {"asset_code": "ALIBABA", "event_type": "tender_offer"}]
    text = formatters.format_scan_summary(stats, new_events=events)
    assert "✅ <b>Проверка завершена</b>" in text
    assert "• проверено источников: 42" in text
    assert "• скачано публикаций: 142" in text
    assert "├ RSS: 100" in text and "├ HTML: 40" in text and "└ Tavily: 2" in text
    assert "• отсечено кэшем: 30" in text
    assert "• новых после кэша: 112" in text
    assert "• прошло keyword-фильтр: 12" in text
    assert "├ high: 3" in text and "├ medium: 2" in text
    assert "├ low: 1" in text and "└ unmatched: 4" in text
    assert "• передано в Gemini: 5" in text
    assert "• вызовов Gemini: 1" in text
    assert "• стоимость: $0.00420" in text
    assert "🔴 SPYF — дробление акций" in text
    assert "🟠 ALIBABA — тендерное предложение" in text


def test_scan_summary_explains_zero_llm():
    """10 keyword-кандидатов и 0 в Gemini — сводка обязана это объяснить."""
    stats = ScanStats(sources_checked=65, publications=1098, after_keywords=10,
                      matched_low=2, unmatched=8, after_filter=0, llm_calls=0)
    text = formatters.format_scan_summary(stats)
    assert "• прошло keyword-фильтр: 10" in text
    assert "├ low: 2" in text and "└ unmatched: 8" in text
    assert "• передано в Gemini: 0" in text
    assert "10 публикаций прошли фильтр, но ни одна не достигла уровня high/medium" in text
    assert "Новых событий не обнаружено." in text


def test_cost_report_format():
    costs = {
        "today": {"calls": 3, "input_tokens": 25000, "output_tokens": 900, "cost_usd": 0.0098, "cache_hits": 40},
        "month": {"calls": 90, "input_tokens": 750000, "output_tokens": 27000, "cost_usd": 0.2925, "cache_hits": 1200},
        "total": {"calls": 90, "input_tokens": 750000, "output_tokens": 27000, "cost_usd": 0.2925, "cache_hits": 1200},
    }
    text = formatters.format_cost_report(costs, "gemini-2.5-flash")
    assert "💰 <b>Стоимость LLM</b>" in text
    assert "<b>Сегодня</b>" in text and "$0.0098" in text
    assert "<b>Текущий месяц</b>" in text and "$0.2925" in text
    assert "Экономия за счёт кэша" in text


def test_digest_format():
    events = [{**FULL_EVENT, "id": 17}]
    text = formatters.format_digest(events)
    assert "🗂 <b>Сводка событий</b>" in text
    assert "Период: последние 24 часа" in text
    assert "🔴 Критические — 1" in text
    assert "1. <b>SPYF</b> — дробление акций" in text
    assert "/event 17" in text


def test_digest_empty():
    last_run = {"finished_at": "2026-07-14 10:00:00", "assets_scanned": 36, "errors": 0}
    text = formatters.format_digest([], last_run=last_run)
    assert "За последние 24 часа новых событий не обнаружено." in text
    assert "Последняя успешная проверка: 14 июля 2026, 13:00 МСК" in text
    assert "Проверено активов: 36" in text
    assert "Ошибок: 0" in text


def test_strip_html():
    html_text = '<b>Жирный</b> и <a href="https://x.com">ссылка</a> &amp; символ'
    assert formatters.strip_html(html_text) == "Жирный и ссылка & символ"


@pytest.mark.asyncio
async def test_reply_html_falls_back_to_plain_text():
    calls = []

    async def reply_text(text, **kwargs):
        calls.append((text, kwargs.get("parse_mode")))
        if kwargs.get("parse_mode") == "HTML":
            raise telegram.error.BadRequest("Can't parse entities")

    message = SimpleNamespace(reply_text=AsyncMock(side_effect=reply_text))
    await reply_html(message, "<b>текст</b> с &lt;экранированием&gt; &amp; сущностями")
    modes = [m for _, m in calls]
    assert modes == ["HTML", None], "после ошибки HTML должна быть отправка обычным текстом"
    assert calls[1][0] == "текст с <экранированием> & сущностями"
