from __future__ import annotations

from app.llm import _parse_json_array
from app.monitor import _fingerprint


def test_parse_plain_json_array():
    assert _parse_json_array('[{"index": 0, "is_event": true}]') == [{"index": 0, "is_event": True}]


def test_parse_array_with_markdown_fence():
    text = '```json\n[{"index": 1, "event_type": "stock_split"}]\n```'
    assert _parse_json_array(text) == [{"index": 1, "event_type": "stock_split"}]


def test_parse_array_with_surrounding_text():
    assert _parse_json_array('Вот результат: [] — конец.') == []


def test_parse_garbage_returns_empty():
    assert _parse_json_array("никакого json здесь нет") == []


def test_parse_invalid_json_returns_empty():
    assert _parse_json_array('[{"index": unterminated') == []


def test_parse_skips_non_dict_items():
    assert _parse_json_array('[{"index": 0}, "мусор", 42]') == [{"index": 0}]


def test_fingerprint_is_stable_and_distinct():
    event = {"event_type": "stock_split", "event_date": "2026-07-01",
             "title": "TSMC delisting notice!", "source_url": "https://x.com/a/b/"}
    assert _fingerprint("TSM", event) == _fingerprint("TSM", event)
    assert _fingerprint("TSM", event) != _fingerprint("AAPL", event)
    # Незначащие отличия (регистр, пунктуация, слэш в конце URL) не меняют отпечаток.
    variant = {**event, "title": "tsmc DELISTING, notice", "source_url": "https://x.com/a/b"}
    assert _fingerprint("TSM", event) == _fingerprint("tsm", variant)
