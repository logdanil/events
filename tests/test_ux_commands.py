"""15 обязательных проверок UX-исправлений (секция 12 ТЗ)."""
from __future__ import annotations

import pytest

from app import bot, db, matcher
from app.llm import LLMProvider, LLMUsage
from app.monitor import Monitor, ScanStats
from tests.test_bot import make_context, make_update


def _seed_assets():
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust (SPY)", "ETF", aliases=["SPY"])
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR", aliases=["TSMC"])
    db.upsert_asset("ALIBABA", "Alibaba Group Holding Ltd – ADR (BABA)", "ADR", aliases=["BABA"])


def _seed_event(code: str, title: str, is_test: int = 0, url: str = "") -> int:
    event = {"fingerprint": f"fp-{code}-{title}", "asset_code": code, "event_type": "stock_split",
             "title": title, "summary_ru": title, "source_url": url or f"https://x.com/{code}",
             "confidence": 0.9, "is_test": is_test}
    return db.save_event(event)


def _replies(update) -> str:
    return "\n".join(c.args[0] for c in update.effective_message.reply_text.await_args_list)


# 1. /events SPYF не возвращает TSM.
@pytest.mark.asyncio
async def test_events_filtered_by_asset(tmp_db):
    _seed_assets()
    _seed_event("SPYF", "Событие SPYF")
    _seed_event("TSM", "Событие TSM")
    update = make_update()
    await bot.events_history(update, make_context(["SPYF"]))
    text = _replies(update)
    assert "SPYF" in text and "TSM" not in text


# 2. /events без аргументов — все активы; лимит числом работает.
@pytest.mark.asyncio
async def test_events_all_assets_and_limit(tmp_db):
    _seed_assets()
    _seed_event("SPYF", "Событие SPYF")
    _seed_event("TSM", "Событие TSM")
    update = make_update()
    await bot.events_history(update, make_context([]))
    text = _replies(update)
    assert "SPYF" in text and "TSM" in text
    update2 = make_update()
    await bot.events_history(update2, make_context(["SPYF", "50"]))
    assert "SPYF" in _replies(update2)


@pytest.mark.asyncio
async def test_events_empty_asset_message(tmp_db):
    _seed_assets()
    update = make_update()
    await bot.events_history(update, make_context(["SPYF"]))
    assert "По активу SPYF сохранённых событий пока нет" in _replies(update)


# 3–4. /event ID и /event TICKER (в т.ч. по алиасу SPY).
@pytest.mark.asyncio
async def test_event_by_id_and_ticker(tmp_db):
    _seed_assets()
    event_id = _seed_event("SPYF", "Дробление SPYF")
    update = make_update()
    await bot.show_event(update, make_context([str(event_id)]))
    assert "Дробление SPYF" in _replies(update)
    update2 = make_update()
    await bot.show_event(update2, make_context(["SPY"]))
    assert "Дробление SPYF" in _replies(update2)
    update3 = make_update()
    await bot.show_event(update3, make_context(["абракадабра"]))
    assert "Использование: /event ID или /event TICKER" in _replies(update3)


# 5. Тестовые события скрыты из рабочего /events, видны в /events test.
@pytest.mark.asyncio
async def test_test_events_hidden(tmp_db):
    _seed_assets()
    _seed_event("SPYF", "Рабочее событие")
    _seed_event("TSM", "Тестовое событие TSM", is_test=1)
    update = make_update()
    await bot.events_history(update, make_context([]))
    text = _replies(update)
    assert "Рабочее событие" in text and "Тестовое событие TSM" not in text
    update2 = make_update()
    await bot.events_history(update2, make_context(["test"]))
    assert "Тестовое событие TSM" in _replies(update2)
    assert db.events_since_hours(24) and all(not e["is_test"] for e in db.events_since_hours(24))


# 6. /rematch SPYF не выдаёт совпадения других активов.
@pytest.mark.asyncio
async def test_rematch_asset_specific(tmp_db):
    _seed_assets()
    db.remember_article("https://c.com/1", matcher.url_hash("https://c.com/1"),
                        matcher.title_hash("Alibaba Group Holding announces tender offer"),
                        None, "s", "skipped:no_asset_match", status="unmatched",
                        title="Alibaba Group Holding announces tender offer", text="")
    update = make_update()
    await bot.rematch(update, make_context(["SPYF", "7"]))
    text = _replies(update)
    assert "Пересопоставление SPYF" in text
    assert "добавлено в очередь на анализ: 0" in text
    assert db.pending_matched_publications() == []
    # /rematch all находит ALIBABA.
    update2 = make_update()
    await bot.rematch(update2, make_context(["all", "7"]))
    assert "добавлено в очередь на анализ: 1" in _replies(update2)


# 7. High/medium после rematch получает pending_llm.
@pytest.mark.asyncio
async def test_rematch_sets_pending_llm(tmp_db):
    _seed_assets()
    db.remember_article("https://c.com/2", matcher.url_hash("https://c.com/2"),
                        matcher.title_hash("SPDR S&P 500 ETF Trust announces stock split"),
                        None, "s", "skipped:no_asset_match", status="unmatched",
                        title="SPDR S&P 500 ETF Trust announces stock split", text="")
    update = make_update()
    await bot.rematch(update, make_context(["SPYF", "7"]))
    pending = db.pending_matched_publications()
    assert len(pending) == 1
    assert pending[0]["best_asset"] == "SPYF"
    assert pending[0]["status"] in ("rematch_high", "rematch_medium")
    # Публикация из очереди не считается «уже обработанной» и не скачивается заново.
    assert db.article_seen(pending[0]["url_hash"]) is True


class SplitProvider(LLMProvider):
    name, model = "fake", "fake-1"

    async def classify_batch(self, items):
        return ([{"index": it["index"], "is_event": True, "event_type": "stock_split",
                  "severity": "critical", "confidence": 0.95, "status": "confirmed",
                  "summary": "Дробление."} for it in items],
                LLMUsage(calls=1, input_tokens=50, output_tokens=5, cost_usd=0.001))


class FailingProvider(LLMProvider):
    name, model = "fake", "fake-1"

    async def classify_batch(self, items):
        raise RuntimeError("Gemini down")


# 8. /processqueue обрабатывает pending_llm (ядро: classify снимает флаг и создаёт событие).
@pytest.mark.asyncio
async def test_pending_queue_processed(tmp_db):
    _seed_assets()
    db.remember_article("https://c.com/3", matcher.url_hash("https://c.com/3"), None,
                        None, "s", "pending", status="rematch_high",
                        title="SPDR S&P 500 ETF Trust stock split", text="",
                        best_asset="SPYF", best_score=0.9, pending_llm=True)
    rows = db.pending_matched_publications()
    assert len(rows) == 1
    m = Monitor.__new__(Monitor)
    m.llm = SplitProvider()
    stats = ScanStats()
    filtered = [{"url": r["url"], "url_hash": r["url_hash"], "title_hash": r["title_hash"],
                 "title": r["title"], "text": r["text"] or "", "published_at": None,
                 "source": r["source"], "asset_codes": [r["best_asset"]]} for r in rows]
    events = await m.classify(filtered, stats)
    saved = m.save_events(events, stats)
    assert len(saved) == 1 and saved[0]["asset_code"] == "SPYF"
    assert db.pending_matched_publications() == [], "после анализа pending_llm снимается"


# 9–10. Счётчики: fetched_total = RSS + HTML, не смешивается с after_cache.
@pytest.mark.asyncio
async def test_fetched_total_not_zero_when_sources_return(tmp_db, monkeypatch):
    from app import html_monitor, rss_monitor

    async def fake_rss(client, sources):
        return [{"url": f"https://r.com/{i}", "title": f"rss {i}", "text": "",
                 "published_at": None, "published_dt": None, "source": "rss",
                 "source_id": 1, "source_type": "RSS", "subscribers": []} for i in range(150)]

    async def fake_html(client, sources):
        return [{"url": f"https://h.com/{i}", "title": f"html {i}", "text": "",
                 "published_at": None, "published_dt": None, "source": "html",
                 "source_id": 2, "source_type": "HTML", "subscribers": []} for i in range(34)]

    monkeypatch.setattr(rss_monitor, "fetch_all", fake_rss)
    monkeypatch.setattr(html_monitor, "fetch_all", fake_html)
    m = Monitor.__new__(Monitor)
    m.http = None
    stats = ScanStats()
    pubs = await m.collect_publications(
        [{"id": 1, "type": "RSS", "parser": "rss", "url": "https://r.com/rss", "subscribers": []},
         {"id": 2, "type": "IR", "parser": "html", "url": "https://h.com", "subscribers": []}], stats)
    assert stats.publications == 184, "fetched_total = 150 RSS + 34 HTML"
    assert stats.rss_publications == 150 and stats.html_publications == 34
    assert stats.cache_hits == 0 and stats.new_after_cache == 0, "кэш-счётчики отдельны от fetched"
    assert len(pubs) == 184


# 11–12. Low/unmatched не идут в Gemini, high/medium идут.
def test_only_confident_matches_sent_to_llm(tmp_db):
    _seed_assets()
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    pubs = [
        {"url": "https://a.com/hi", "title": "SPDR S&P 500 ETF Trust announces stock split",
         "text": "", "published_at": None, "published_dt": None, "source": "s", "subscribers": []},
        {"url": "https://a.com/low", "title": "State Street merger rumors circulate",
         "text": "", "published_at": None, "published_dt": None, "source": "s", "subscribers": []},
    ]
    stats = ScanStats()
    kept = matcher.filter_publications(pubs, profiles, lookback_days=3, stats=stats)
    assert [p["url"] for p in kept] == ["https://a.com/hi"], "в Gemini уходит только high/medium"
    assert stats.matched_high == 1
    # Вторая публикация не прошла порог: low/unmatched либо irrelevant_global (нет кандидатов).
    assert stats.matched_low + stats.unmatched + stats.irrelevant_global == 1


# 13. Чужие SEC filings → irrelevant_global, не пользовательский unmatched.
def test_foreign_sec_filing_irrelevant_global(tmp_db):
    _seed_assets()
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    pub = {"url": "https://sec.gov/x", "title": "8-K - ACME ROBOTICS INC (0009999999) (Filer)",
           "text": "", "published_at": None, "published_dt": None, "source": "SEC",
           "subscribers": [], "sec_filer": "ACME ROBOTICS INC", "sec_cik": "0009999999"}
    stats = ScanStats()
    kept = matcher.filter_publications([pub], profiles, lookback_days=3, stats=stats)
    assert kept == []
    assert stats.irrelevant_global == 1 and stats.unmatched == 0
    assert db.list_unmatched(limit=10) == [], "чужой SEC-филинг не засоряет /unmatched"
    article = db.get_article(matcher.url_hash("https://sec.gov/x"))
    assert article["status"] == "irrelevant_global"


# 14. Ошибка Gemini сохраняет pending_llm для повторной попытки.
@pytest.mark.asyncio
async def test_llm_failure_keeps_pending(tmp_db):
    _seed_assets()
    db.remember_article("https://c.com/4", matcher.url_hash("https://c.com/4"), None,
                        None, "s", "pending", status="rematch_high",
                        title="SPDR S&P 500 ETF Trust stock split", text="",
                        best_asset="SPYF", best_score=0.9, pending_llm=True)
    m = Monitor.__new__(Monitor)
    m.llm = FailingProvider()
    rows = db.pending_matched_publications()
    stats = ScanStats()
    filtered = [{"url": r["url"], "url_hash": r["url_hash"], "title_hash": r["title_hash"],
                 "title": r["title"], "text": "", "published_at": None,
                 "source": r["source"], "asset_codes": [r["best_asset"]]} for r in rows]
    events = await m.classify(filtered, stats)
    assert events == [] and stats.llm_failed == 1
    assert len(db.pending_matched_publications()) == 1, "сбой LLM не теряет очередь"


# 16. Tavily-результат не «принадлежит» активу: без текстового совпадения — не матч.
def test_search_results_do_not_get_ownership(tmp_db):
    _seed_assets()
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    # Чужая новость с корп. ключевыми словами, найденная запросом по SPYF.
    pub = {"title": "Malibu Life Holdings completes acquisition of TruSpire", "text": "",
           "source_type": "SEARCH", "source": "Tavily", "subscribers": ["SPYF"]}
    assert matcher.match_assets(pub, profiles) == [], \
        "поисковый результат без упоминания актива не должен сопоставляться"
    # Персональная страница фонда, но новость о посторонней компании без единого
    # слова о фонде — это новость о составе индекса, фонд её не получает.
    pub_ir = {**pub, "source_type": "IR", "source_id": 5}
    assert matcher.match_assets(pub_ir, profiles) == []
    # А уведомление самого фонда на его странице — владение сохраняет.
    pub_fund = {**pub_ir, "title": "Notice to unitholders: the Trust announces distribution and unit split"}
    assert matcher.match_assets(pub_fund, profiles) == ["SPYF"]
    # Tavily-результат, реально упоминающий актив, матчится по тексту.
    pub_real = {"title": "SPDR S&P 500 ETF Trust announces stock split", "text": "",
                "source_type": "SEARCH", "subscribers": ["SPYF"]}
    assert matcher.match_assets(pub_real, profiles) == ["SPYF"]


# 17. Модель получает актив-кандидат для каждой публикации.
@pytest.mark.asyncio
async def test_classify_passes_candidate_asset_to_llm(tmp_db):
    _seed_assets()

    class CapturingProvider(LLMProvider):
        name, model = "fake", "fake-1"
        captured: list = []

        async def classify_batch(self, items):
            self.captured.extend(items)
            return ([{"index": it["index"], "is_event": False} for it in items],
                    LLMUsage(calls=1, input_tokens=1, output_tokens=1, cost_usd=0))

    m = Monitor.__new__(Monitor)
    m.llm = CapturingProvider()
    pub = {"url": "https://x.com/1", "url_hash": matcher.url_hash("https://x.com/1"),
           "title_hash": None, "title": "SPY split", "text": "", "published_at": None,
           "source": "s", "asset_codes": ["SPYF"]}
    await m.classify([pub], ScanStats())
    assert m.llm.captured[0]["asset"].startswith("SPYF — SPDR S&P 500 ETF Trust")


# 15. /pipetest mock не создаёт рабочее событие.
@pytest.mark.asyncio
async def test_pipetest_mock_creates_only_test_event(tmp_db, monkeypatch):
    from app import main as app_main
    _seed_assets()
    update = make_update()
    context = make_context(["SPYF", "mock"])
    await app_main.pipe_test(update, context)
    text = _replies(update)
    assert "Keyword-фильтр: пройден" in text
    assert "Gemini: пропущен (mock)" in text
    assert "is_test=1" in text
    assert db.recent_events() == [], "рабочий список событий пуст"
    test_events = db.recent_events(test_only=True)
    assert len(test_events) == 1 and test_events[0]["asset_code"] == "SPYF"

def test_news_aggregator_feed_gives_no_ownership(tmp_db):
    """Статья про SK Hynix из Google-ленты, подписанной на NASD, не должна лечь на NASD."""
    db.upsert_asset("NASD", "Invesco QQQ Trust, Series 1 (QQQ)", "ETF", aliases=["QQQ"])
    db.upsert_asset("HYNIX", "Акции SK Hynix", "SHARE", aliases=["SK Hynix"])
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    pub = {"title": "SK Hynix climbs 4% on record buyback", "text": "",
           "source_type": "NEWS", "source": "Google News", "source_id": 77, "subscribers": ["NASD"]}
    codes = matcher.match_assets(pub, profiles)
    assert codes == ["HYNIX"], f"ожидали HYNIX по тексту, получили {codes}"


def test_constituent_news_belongs_to_company_not_fund(tmp_db):
    """Эмиссия Alibaba со страницы биржи, на которую подписан Tracker Fund,
    — событие ALIBABA, а не HANG (регресс события #15)."""
    db.upsert_asset("HANG", "Tracker Fund of Hong Kong", "ETF", aliases=["Tracker Fund"])
    db.upsert_asset("ALIBABA", "Alibaba Group Holding Ltd – ADR (BABA)", "ADR", aliases=["Alibaba"])
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    pub = {"title": "Alibaba Group Holding raises US$10.2 billion in share issuance for AI",
           "text": "", "source_type": "EXCHANGE", "source": "HKEX", "source_id": 9,
           "subscribers": ["HANG"]}
    results = matcher.match_publication(pub, profiles)
    assert [r.asset_code for r in results] == ["ALIBABA"]


def test_company_news_does_not_leak_to_index_fund(tmp_db):
    """Новость о Novartis не относится к DAX, даже если слово DAX встречается рядом."""
    db.upsert_asset("DAX", "iShares Core DAX UCITS ETF", "ETF", aliases=["DAX"])
    db.upsert_asset("NOVARTIS", "Novartis AG", "ADR", aliases=["Novartis", "NVS"])
    profiles = matcher.build_profiles(db.list_assets(enabled_only=True))
    pub = {"title": "Novartis shares rise on dual listing plan; DAX index flat",
           "text": "", "source_type": "NEWS", "source": "Google News", "subscribers": ["DAX"]}
    assert [r.asset_code for r in matcher.match_publication(pub, profiles)] == ["NOVARTIS"]


class TargetProvider(LLMProvider):
    name, model = "fake", "fake-1"

    async def classify_batch(self, items):
        from datetime import date
        return ([{"index": it["index"], "asset_code": "ALIBABA", "about_asset": True,
                  "is_event": True, "event_type": "share_issuance", "severity": "high",
                  "confidence": 0.9, "status": "confirmed", "publication_date": date.today().isoformat(),
                  "summary": "Эмиссия."} for it in items],
                LLMUsage(calls=1, input_tokens=50, output_tokens=5, cost_usd=0.001))


@pytest.mark.asyncio
async def test_one_publication_one_event_for_named_asset(tmp_db):
    """Публикация с несколькими кандидатами → событие только у актива, названного моделью."""
    _seed_assets()
    db.upsert_asset("HANG", "Tracker Fund of Hong Kong", "ETF")
    m = Monitor.__new__(Monitor)
    m.llm = TargetProvider()
    pub = {"url": "https://x.com/1", "url_hash": matcher.url_hash("https://x.com/1"),
           "title_hash": None, "title": "Alibaba raises US$10.2bn", "text": "",
           "published_at": None, "source": "HKEX", "asset_codes": ["HANG", "ALIBABA"]}
    events = await m.classify([pub], ScanStats())
    assert [e["asset_code"] for e in events] == ["ALIBABA"]
