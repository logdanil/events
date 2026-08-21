from __future__ import annotations

from app.monitor import MIN_CONFIDENCE, _normalize_event, classification_ok, split_sources


def test_classification_ok_rules():
    good = {"is_event": True, "event_type": "stock_split", "confidence": 0.9, "status": "confirmed"}
    assert classification_ok(good) == (True, "")
    assert classification_ok({**good, "is_event": False})[0] is False
    assert classification_ok({**good, "event_type": "other"})[0] is False
    assert classification_ok({**good, "event_type": "выдумка"})[0] is False
    assert classification_ok({**good, "confidence": 0.5})[0] is False
    assert classification_ok({**good, "confidence": 0.5}, min_confidence=0.0)[0] is True
    # Слухи не рассылаются, но в debug-режиме допускаются.
    rumor = {**good, "status": "rumor"}
    assert classification_ok(rumor)[0] is False
    assert classification_ok(rumor, allow_rumors=True)[0] is True


def test_split_sources_cascade():
    sources = [
        {"type": "SEC", "parser": "auto", "url": "https://sec.gov/x?output=atom"},
        {"type": "IR", "parser": "auto", "url": "https://company.com/rss.xml"},
        {"type": "IR", "parser": "auto", "url": "https://company.com/investors"},
        {"type": "EXCHANGE", "parser": "rss", "url": "https://exchange.com/whatever"},
        {"type": "EXCHANGE", "parser": "html", "url": "https://exchange.com/feed"},
    ]
    rss, html = split_sources(sources)
    assert [s["url"] for s in html] == ["https://company.com/investors", "https://exchange.com/feed"]
    assert len(rss) == 3


def test_normalize_event_from_llm_classification():
    event = _normalize_event("SPYF", {
        "is_event": True, "event_type": "stock_split", "severity": "critical",
        "confidence": 0.95, "summary": "Дробление 1:5.", "impact": "Цена снизится кратно.",
        "event_date": "2026-07-21", "status": "confirmed", "action_required": True,
        "key_parameters": [{"label": "Коэффициент", "value": "1:5"}],
        "title": "Issuer announces 5-for-1 split",
        "sources": [{"name": "IR", "url": "https://x.com/press/1"}],
    })
    assert event["asset_code"] == "SPYF"
    assert event["severity"] == "CRITICAL"
    assert event["summary_ru"] == "Дробление 1:5."
    assert event["impact_ru"] == "Цена снизится кратно."
    assert event["source_url"] == "https://x.com/press/1"
    assert '"1:5"' in event["key_parameters_json"]
    assert event["fingerprint"]
    assert float(event["confidence"]) == 0.95


def test_stale_and_foreign_publications_rejected():
    from datetime import date, timedelta
    base = {"is_event": True, "event_type": "stock_split", "confidence": 0.9, "status": "confirmed"}
    old = (date.today() - timedelta(days=55)).isoformat()
    fresh = (date.today() - timedelta(days=2)).isoformat()
    future = (date.today() + timedelta(days=30)).isoformat()
    # Июньская статья в августе — устаревшая, даже если лента отдала её как новую.
    ok, reason = classification_ok({**base, "publication_date": old})
    assert ok is False and "устаревшая" in reason
    # Свежая публикация — проходит; будущая дата события — тоже (ещё предстоит).
    assert classification_ok({**base, "publication_date": fresh})[0] is True
    assert classification_ok({**base, "event_date": future})[0] is True
    # Модель сказала «не об этом активе» — отклоняем независимо от остального.
    ok, reason = classification_ok({**base, "about_asset": False, "publication_date": fresh})
    assert ok is False and "не об этом активе" in reason
