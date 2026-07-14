from __future__ import annotations

import pytest

from app import db, matcher, rss_monitor
from app.llm import LLMProvider, LLMUsage, classify_publications
from app.monitor import Monitor, ScanStats

# --- Кэш обработанных публикаций -------------------------------------------

def test_article_cache_by_url_and_title(tmp_db):
    uh, th = matcher.url_hash("https://x.com/a"), matcher.title_hash("Big Split News")
    assert db.article_seen(uh, th) is False
    db.remember_article("https://x.com/a", uh, th, None, "src", "stock_split")
    assert db.article_seen(uh) is True, "повторный URL не должен обрабатываться"
    other_url = matcher.url_hash("https://y.com/b")
    assert db.article_seen(other_url, th) is True, "тот же заголовок с другого URL — тоже кэш"
    assert db.article_seen(other_url, matcher.title_hash("Другой заголовок")) is False


def test_url_hash_normalization():
    assert matcher.url_hash("https://www.X.com/a/") == matcher.url_hash("http://x.com/a")
    assert matcher.title_hash("Big  Split,  News!") == matcher.title_hash("big split news")


# --- RSS --------------------------------------------------------------------

SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test IR</title>
<item><title>Company announces 5-for-1 stock split</title>
<link>https://ir.example.com/press/split</link>
<description>Board approved a five-for-one split. Record date July 18, 2026.</description>
<pubDate>Mon, 13 Jul 2026 09:00:00 GMT</pubDate></item>
<item><title>Quarterly newsletter</title><link>https://ir.example.com/press/news</link>
<pubDate>Mon, 13 Jul 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_rss_parse_feed():
    source = {"id": 1, "name": "Test IR", "type": "RSS", "url": "https://ir.example.com/rss",
              "subscribers": ["SPYF"]}
    pubs = rss_monitor.parse_feed(SAMPLE_RSS, source)
    assert len(pubs) == 2
    first = pubs[0]
    assert first["title"] == "Company announces 5-for-1 stock split"
    assert first["url"] == "https://ir.example.com/press/split"
    assert "Record date" in first["text"]
    assert first["published_dt"] is not None and first["published_dt"].year == 2026
    assert first["subscribers"] == ["SPYF"]


# --- Matcher ----------------------------------------------------------------

def _assets():
    return [
        {"code": "SPYF", "name": "SPDR S&P 500 ETF Trust (SPY)", "aliases": ["SPY"]},
        {"code": "ALIBABA", "name": "Alibaba Group Holding Ltd – ADR (BABA)", "aliases": ["BABA"]},
    ]


def test_keyword_filter():
    assert matcher.has_corporate_keywords({"title": "Company announces reverse split", "text": ""})
    assert matcher.has_corporate_keywords({"title": "New 8-K filing", "text": ""})
    assert not matcher.has_corporate_keywords({"title": "Stock rises 3% on earnings beat rumors", "text": ""})


def test_match_assets_by_name_and_alias():
    profiles = matcher.build_profiles(_assets())
    pub = {"title": "Alibaba Group Holding announces tender offer", "text": ""}
    assert matcher.match_assets(pub, profiles) == ["ALIBABA"]
    pub2 = {"title": "SPY ETF: 5-for-1 unit split declared", "text": ""}
    assert matcher.match_assets(pub2, profiles) == ["SPYF"]
    # Короткий тикер не должен ловиться внутри слова.
    pub3 = {"title": "spyware vendor delisted", "text": ""}
    assert matcher.match_assets(pub3, profiles) == []


def test_match_assets_falls_back_to_subscribers():
    profiles = matcher.build_profiles(_assets())
    # Персональная страница актива + корпоративные ключевые слова → medium.
    pub = {"title": "Notice of unit split", "text": "", "source_id": 5}
    assert matcher.match_assets(pub, profiles, subscribers=["SPYF"]) == ["SPYF"]


def test_filter_publications_pipeline(tmp_db):
    index = matcher.build_profiles(_assets())
    pubs = [
        {"url": "https://a.com/1", "title": "Alibaba announces ADR ratio change", "text": "",
         "published_at": None, "published_dt": None, "source": "s", "subscribers": []},
        {"url": "https://a.com/1", "title": "Alibaba announces ADR ratio change", "text": "",
         "published_at": None, "published_dt": None, "source": "s", "subscribers": []},  # дубль URL
        {"url": "https://a.com/2", "title": "Гороскоп на завтра", "text": "",
         "published_at": None, "published_dt": None, "source": "s", "subscribers": []},  # нет ключевых слов
        {"url": "https://a.com/3", "title": "Some company merger announced", "text": "",
         "published_at": None, "published_dt": None, "source": "s", "subscribers": []},  # нет актива
    ]
    stats = ScanStats()
    kept = matcher.filter_publications(pubs, index, lookback_days=3, stats=stats)
    assert len(kept) == 1
    assert kept[0]["asset_codes"] == ["ALIBABA"]
    # Прошедшая фильтр публикация кэшируется после классификации (эмулируем её),
    # отфильтрованные — сразу; второй проход всё отдаёт в кэш-попадания.
    db.remember_article(kept[0]["url"], kept[0]["url_hash"], kept[0]["title_hash"],
                        None, "s", "adr_ratio_change")
    stats2 = ScanStats()
    kept2 = matcher.filter_publications(pubs, index, lookback_days=3, stats=stats2)
    assert kept2 == []
    assert stats2.cache_hits == 3


# --- Батчинг LLM --------------------------------------------------------------

class FakeProvider(LLMProvider):
    name = "fake"
    model = "fake-1"

    def __init__(self):
        self.batches: list[int] = []

    async def classify_batch(self, items):
        self.batches.append(len(items))
        results = [{"index": it["index"], "is_event": False} for it in items]
        return results, LLMUsage(calls=1, input_tokens=100, output_tokens=10, cost_usd=0.001)


@pytest.mark.asyncio
async def test_llm_batching_splits_publications():
    provider = FakeProvider()
    items = [{"index": i, "title": f"t{i}", "url": f"u{i}", "text": ""} for i in range(45)]
    by_index, usage, failed = await classify_publications(provider, items, batch_size=20)
    assert provider.batches == [20, 20, 5], "45 публикаций → 3 вызова, не 45"
    assert usage.calls == 3
    assert len(by_index) == 45
    assert failed == set()


# --- Дедупликация событий на выходе конвейера ---------------------------------

class SplitProvider(FakeProvider):
    async def classify_batch(self, items):
        results = [{"index": it["index"], "is_event": True, "event_type": "stock_split",
                    "severity": "critical", "confidence": 0.95, "status": "confirmed",
                    "summary": "Дробление 1:5.", "event_date": "2026-07-21"} for it in items]
        return results, LLMUsage(calls=1, input_tokens=100, output_tokens=10, cost_usd=0.001)


@pytest.mark.asyncio
async def test_semantic_event_dedup_across_articles(tmp_db):
    """Четыре разные статьи об одном листинге → одно событие, не четыре уведомления."""
    db.upsert_asset("HYNIX", "Акции SK Hynix", "SHARE", aliases=["SK Hynix"])
    monitor = Monitor.__new__(Monitor)
    monitor.llm = SplitProvider()  # каждая статья классифицируется как событие
    stats = ScanStats()
    titles = [
        "SK Hynix raised $26.5 billion in Wall Street listing",
        "SK Hynix shares slide after stellar Nasdaq debut",
        "Analysts discuss SK Hynix ADR listing impact",
        "Gabelli Funds on upcoming SK Hynix listing",
    ]
    pubs = [{"url": f"https://x.com/{i}", "title": t, "text": "", "published_at": None,
             "source": "Tavily", "url_hash": matcher.url_hash(f"https://x.com/{i}"),
             "title_hash": matcher.title_hash(t), "asset_codes": ["HYNIX"]}
            for i, t in enumerate(titles)]
    events = await monitor.classify(pubs, stats)
    saved = monitor.save_events(events, stats)
    assert len(saved) == 1, "одно событие вместо четырёх"
    assert stats.duplicates == 3
    assert db.similar_event_exists("HYNIX", "stock_split") is True
    # Следующий скан с новой статьёй о том же — тоже дубликат.
    pub5 = {**pubs[0], "url": "https://y.com/5", "url_hash": matcher.url_hash("https://y.com/5"),
            "title": "More coverage of SK Hynix listing", "title_hash": matcher.title_hash("More coverage")}
    events5 = await monitor.classify([pub5], stats)
    assert monitor.save_events(events5, stats) == []


@pytest.mark.asyncio
async def test_pipeline_classify_and_event_dedup(tmp_db):
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust", "ETF", aliases=["SPY"])
    monitor = Monitor.__new__(Monitor)  # без сетевых клиентов
    monitor.llm = SplitProvider()
    pub = {"url": "https://ir.example.com/split", "title": "SPY announces 5-for-1 split",
           "text": "", "published_at": None, "source": "Test IR",
           "url_hash": matcher.url_hash("https://ir.example.com/split"),
           "title_hash": matcher.title_hash("SPY announces 5-for-1 split"),
           "asset_codes": ["SPYF"]}
    stats = ScanStats()
    events = await monitor.classify([pub], stats)
    assert len(events) == 1
    saved = monitor.save_events(events, stats)
    assert len(saved) == 1 and stats.saved == 1
    # Повторная классификация того же события → дубликат, не новое уведомление.
    events2 = await monitor.classify([pub], stats)
    saved2 = monitor.save_events(events2, stats)
    assert saved2 == [] and stats.duplicates == 1
    # Статья запомнена в кэше с результатом классификации.
    assert db.article_seen(pub["url_hash"]) is True
