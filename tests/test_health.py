"""Контроль здоровья: инциденты, дедупликация, источники, планировщик, доставка."""
from __future__ import annotations

import pytest

from app import bot, db, health
from app.monitor import ScanStats
from tests.test_bot import make_context, make_update


def _text(update) -> str:
    return "\n".join(c.args[0] for c in update.effective_message.reply_text.await_args_list)


def test_report_dedup_and_resolve(tmp_db):
    a1 = health.report("llm", "unavailable", "Gemini 503", "critical")
    assert a1 and a1["new"] is True
    # Повтор в течение cooldown — молчим, но считаем.
    assert health.report("llm", "unavailable", "Gemini 503", "critical") is None
    incidents = health.open_incidents()
    assert len(incidents) == 1 and incidents[0]["occurrences"] == 2
    # Восстановление — сообщаем (администратор был оповещён).
    r = health.resolve("llm", "unavailable")
    assert r and r["component"] == "llm"
    assert health.open_incidents() == []
    # Повторное восстановление — тишина.
    assert health.resolve("llm", "unavailable") is None
    # Новое падение после восстановления — снова «новая проблема».
    a2 = health.report("llm", "unavailable", "Gemini 503", "critical")
    assert a2 and a2["new"] is True


def test_source_alert_after_threshold(tmp_db):
    db.upsert_asset("SPYF", "SPDR S&P 500 ETF Trust", "ETF")
    db.add_source("SPYF", "https://ir.example.com/news")
    source = next(s for s in db.list_source_defs() if "ir.example.com" in s["url"])
    # Первые сбои — тишина (единичная ошибка сети не повод будить).
    for _ in range(health.SOURCE_FAIL_THRESHOLD - 1):
        assert health.check_source(source, "HTTP 503") is None
    alert = health.check_source(source, "HTTP 503")
    assert alert and "недоступен" in alert["message"] and "SPYF" in alert["message"]
    # Успех — сброс серии и «восстановлено» в буфере.
    health.pending_resolved.clear()
    assert health.check_source(source, None) is None
    assert health.pending_resolved and health.pending_resolved[0]["component"] == "source"
    with db.connect() as conn:
        assert conn.execute("SELECT fail_streak FROM source_defs WHERE id=?",
                            (source["id"],)).fetchone()[0] == 0


def test_scan_totals_checks(tmp_db):
    stats = ScanStats(sources_checked=100, publications=0)
    alerts = health.check_scan_totals(stats)
    assert any(a["key"] == "zero_publications" and a["severity"] == "critical" for a in alerts)
    stats2 = ScanStats(sources_checked=100, publications=1500, llm_failed=7, cost_usd=0.9)
    alerts2 = health.check_scan_totals(stats2)
    keys = {a["key"] for a in alerts2}
    assert "batch_failed" in keys and "cost_spike" in keys
    # zero_publications при этом закрывается.
    assert all(i["key"] != "zero_publications" for i in health.open_incidents())


def test_scheduler_stale(tmp_db):
    run_id = db.start_scan_run()
    db.finish_scan_run(run_id, "ok", 50, 0)
    assert health.check_scheduler(60) is None, "свежая проверка — всё в порядке"
    with db.connect() as conn:
        conn.execute("UPDATE scan_runs SET finished_at=datetime('now','-5 hours') WHERE id=?", (run_id,))
    alert = health.check_scheduler(60)
    assert alert and alert["key"] == "stale" and alert["severity"] == "critical"


def test_delivery_blocked_user_is_info(tmp_db):
    a = health.check_delivery("telegram", "555", RuntimeError("Forbidden: bot was blocked by the user"))
    assert a and a["severity"] == "info" and "заблокировал" in a["message"]
    b = health.check_delivery("telegram", "556", RuntimeError("Timed out"))
    assert b and b["severity"] == "warning"


def test_health_report_and_format(tmp_db):
    health.report("llm", "unavailable", "Gemini 503", "critical")
    text = health.format_health_report(60)
    assert "Здоровье сервиса" in text and "Открытые проблемы (1)" in text and "Gemini 503" in text
    health.resolve("llm", "unavailable")
    assert "Открытых проблем нет" in health.format_health_report(60)
    alert = {"component": "llm", "severity": "critical", "message": "x", "occurrences": 3, "new": False}
    assert "×3" in health.format_alert(alert)


def test_health_report_keeps_source_error_details(tmp_db):
    db.upsert_asset("SAMSUNG", "Samsung Electronics", "SHARE")
    url = "https://news.samsung.com/global/feed"
    db.add_source("SAMSUNG", url)
    source = next(s for s in db.list_source_defs() if s["url"] == url)
    error = ("HTTPStatusError: HTTP 403; "
             "TLS: chrome=HTTP 403, safari=HTTP 403, firefox135=HTTP 403; "
             f"URL: {url}")
    for _ in range(health.SOURCE_FAIL_THRESHOLD):
        health.check_source(source, error)
    text = health.format_health_report(60)
    assert error in text
    assert "Активы: SAMSUNG" in text


def test_health_formatters_escape_dynamic_html(tmp_db):
    message = "Ошибка <feed> & URL ?a=1&b=2"
    alert = health.report("source", "source:999", message)
    assert alert is not None
    escaped = "Ошибка &lt;feed&gt; &amp; URL ?a=1&amp;b=2"
    assert escaped in health.format_alert(alert)
    assert escaped in health.format_health_report(60)
    resolved = health.resolve("source", "source:999")
    assert resolved is not None
    assert escaped in health.format_resolved(resolved)


@pytest.mark.asyncio
async def test_export_import_recipients(tmp_db):
    db.add_subscriber(555, "Иван Петров")
    db.add_subscriber(777)
    db.add_email_recipient("a@x.ru")
    update = make_update()
    await bot.export_recipients(update, make_context([]))
    text = _text(update)
    assert "/importusers 555:Иван_Петров 777" in text
    assert "/addemail a@x.ru" in text
    # Восстановление в пустую базу.
    db.remove_subscriber(555); db.remove_subscriber(777)
    update2 = make_update()
    await bot.import_users(update2, make_context(["555:Иван_Петров", "777", "мусор"]))
    assert "добавлено: 2" in _text(update2)
    subs = {s["chat_id"]: s["name"] for s in db.list_subscribers()}
    assert subs == {555: "Иван Петров", 777: None}


def test_rehabilitate_muted_sources(tmp_db):
    """Источники, отключённые старым авто-приглушением, возвращаются в мониторинг."""
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR")
    db.add_source("TSM", "https://pr.tsmc.com/english/news")
    src = next(s for s in db.list_source_defs() if "pr.tsmc" in s["url"])
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET enabled=0, fail_streak=16 WHERE id=?", (src["id"],))
        conn.execute("INSERT INTO health_incidents (component, key, message, severity, "
                     "first_seen, last_seen) VALUES ('source', ?, 'отключён', 'info', "
                     "'2026-08-19', '2026-08-19')", (f"muted:{src['id']}",))
    messages = health.rehabilitate_sources()
    assert messages and "снова в мониторинге" in messages[0]
    fresh = next(s for s in db.list_source_defs() if s["id"] == src["id"])
    assert fresh["enabled"] == 1 and not fresh.get("fail_streak")
    assert health.rehabilitate_sources() == [], "повторный вызов ничего не трогает"


def test_403_source_stays_enabled(tmp_db):
    """403-источник не отключается: продолжаем проверять (обход по TLS-отпечаткам)."""
    db.upsert_asset("SONY", "Sony Group Corporation", "ADR")
    db.add_source("SONY", "https://blocked.example.com/press")
    db.add_source("SONY", "https://backup.example.com/rss")
    blocked = next(s for s in db.list_source_defs() if "blocked" in s["url"])
    for _ in range(health.SOURCE_FAIL_THRESHOLD * 5):
        health.check_source(blocked, "HTTPStatusError: Client error '403 Forbidden'")
    fresh = next(s for s in db.list_source_defs(enabled_only=False) if s["id"] == blocked["id"])
    assert fresh["enabled"] == 1


def test_url_fix_with_existing_duplicate(tmp_db, monkeypatch):
    """Если рабочий URL уже есть в базе, сломанный источник выключается,
    подписки переезжают — и это не роняет запуск (регресс краш-цикла на Railway)."""
    db.upsert_asset("EM", "iShares MSCI Emerging Markets ETF", "ETF")
    db.add_source("EM", "https://broken.example.com/page")
    db.add_source("EM", "https://working.example.com/page")
    monkeypatch.setattr(health, "URL_FIXES",
                        {"https://broken.example.com/page": "https://working.example.com/page"})
    messages = health.rehabilitate_sources()
    assert messages and "заменён на рабочий" in messages[0]
    rows = {s["url"]: s for s in db.list_source_defs(enabled_only=False)}
    assert rows["https://broken.example.com/page"]["enabled"] == 0
    assert rows["https://working.example.com/page"]["enabled"] == 1
    assert health.rehabilitate_sources() == []


def test_url_fix_with_open_incident_same_transaction(tmp_db, monkeypatch):
    """Регресс Railway: у сломанного источника есть открытый инцидент — реабилитация
    не должна упираться в блокировку базы (второе соединение внутри транзакции)."""
    db.upsert_asset("EM", "iShares MSCI Emerging Markets ETF", "ETF")
    db.add_source("EM", "https://broken.example.com/page")
    db.add_source("EM", "https://working.example.com/page")
    broken = next(s for s in db.list_source_defs() if "broken" in s["url"])
    health.report("source", f"source:{broken['id']}", "недоступен 94 проверок")
    monkeypatch.setattr(health, "URL_FIXES",
                        {"https://broken.example.com/page": "https://working.example.com/page"})
    messages = health.rehabilitate_sources()
    assert messages, "реабилитация должна применяться при открытом инциденте"
    assert all(i["key"] != f"source:{broken['id']}" for i in health.open_incidents())


def test_kraken_blog_replaced_by_google_news(tmp_db):
    """Kraken закрыл блог для программных клиентов (403 на все TLS-отпечатки) —
    реабилитация переводит ленту на Google News, закрывает инцидент и
    растягивает интервал опроса, как у остальных лент Google."""
    for code in ("BTC", "ETH", "SOL", "XRP", "TRX", "BNB"):
        db.upsert_asset(code, f"Индекс МосБиржи {code}", "CRYPTO")
        db.add_source(code, "https://blog.kraken.com/feed")
    src = next(s for s in db.list_source_defs() if "kraken" in s["url"])
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET fail_streak=9 WHERE id=?", (src["id"],))
    health.report("source", f"source:{src['id']}", "недоступен 9 проверок подряд (403)")
    messages = health.rehabilitate_sources()
    assert any("заменён на рабочий" in m for m in messages)
    rows = [s for s in db.list_source_defs(enabled_only=False)]
    assert all("blog.kraken.com/feed" not in s["url"] or not s["enabled"] for s in rows)
    fixed = next(s for s in rows if "site:blog.kraken.com" in s["url"])
    assert fixed["enabled"] == 1 and fixed["fail_streak"] == 0
    assert fixed["refresh_interval"] >= 180, "лента Google опрашивается раз в 3-5 часов"
    assert all(i["key"] != f"source:{src['id']}" for i in health.open_incidents())
    assert health.rehabilitate_sources() == [], "повторный запуск ничего не трогает"


def test_ishares_uk_library_replaced_by_fund_pages(tmp_db):
    """iShares удалил «библиотеку документов» на британском сайте (404) — реабилитация
    переводит STOX/DAX/INDIA на живые страницы фондов и закрывает инциденты."""
    broken = [u for u in health.URL_FIXES if "education/library" in u]
    assert len(broken) == 3, "в URL_FIXES должны быть все три сломанные ссылки iShares"
    codes = ("STOX", "DAX", "INDIA")
    for code, url in zip(codes, broken):
        db.upsert_asset(code, f"iShares {code} ETF", "ETF")
        db.add_source(code, url)
        src = next(s for s in db.list_source_defs() if s["url"] == url)
        health.report("source", f"source:{src['id']}", "недоступен 5 проверок подряд (404)")
    messages = health.rehabilitate_sources()
    assert sum("заменён на рабочий" in m for m in messages) == 3
    urls = {s["url"] for s in db.list_source_defs()}
    for old, new in health.URL_FIXES.items():
        if "education/library" in old:
            assert old not in urls and new in urls
    assert not [i for i in health.open_incidents() if i["component"] == "source"]
    assert health.rehabilitate_sources() == [], "повторный запуск ничего не трогает"


def test_lme_pages_merge_into_shared_circulars_feed(tmp_db):
    """LME блокирует дата-центры: четыре страницы металлов сводятся в одну ленту
    циркуляров Google News, подписки всех металлов сохраняются, инциденты закрыты."""
    broken = [u for u in health.URL_FIXES if "lme.com/" in u and "news.google" not in u]
    assert len(broken) == 4
    codes = ("ALUM", "COPPER", "ZINC", "NICKEL")
    for code, url in zip(codes, broken):
        db.upsert_asset(code, f"LME {code}", "ASSET")
        db.add_source(code, url)
        src = next(s for s in db.list_source_defs() if s["url"] == url)
        health.report("source", f"source:{src['id']}", "недоступен 18 проверок подряд (403)")
    messages = health.rehabilitate_sources()
    assert sum("заменён на рабочий" in m for m in messages) == 4
    feeds = [s for s in db.list_source_defs() if "site:lme.com" in s["url"]]
    assert len(feeds) == 1 and feeds[0]["enabled"] == 1, "одна общая лента вместо четырёх"
    assert set(feeds[0].get("subscribers") or []) >= set(codes), \
        "подписки всех четырёх металлов перенесены на общую ленту"
    assert not [i for i in health.open_incidents() if i["component"] == "source"]
    assert health.rehabilitate_sources() == []


def test_url_fix_closes_disabled_incident(tmp_db, monkeypatch):
    """Регресс TRON: источник авто-отключён за 404 (инцидент disabled:) — замена URL
    включает его обратно и закрывает именно этот инцидент, а не только source:."""
    db.upsert_asset("TRX", "Индекс МосБиржи Трона (MOEXTRX)", "CRYPTO")
    db.add_source("TRX", "https://dead.example.org/news/")
    src = next(s for s in db.list_source_defs() if "dead.example" in s["url"])
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET enabled=0, fail_streak=9 WHERE id=?", (src["id"],))
    health.report("source", f"disabled:{src['id']}", "страница не существует (404, 9 раз)",
                  severity="info")
    monkeypatch.setattr(health, "URL_FIXES",
                        {"https://dead.example.org/news/": "https://alive.example.org/rss"})
    messages = health.rehabilitate_sources()
    assert any("заменён на рабочий" in m for m in messages)
    fixed = next(s for s in db.list_source_defs(enabled_only=False) if s["id"] == src["id"])
    assert fixed["enabled"] == 1 and fixed["url"] == "https://alive.example.org/rss"
    assert all(i["key"] != f"disabled:{src['id']}" for i in health.open_incidents())


def test_already_fixed_url_still_closes_stale_incident(tmp_db, monkeypatch):
    """Регресс TRON: замена уже применена ранее (рабочая лента есть, старый источник
    выключен), но инцидент «отключён за 404» завис — реабилитация обязана его закрыть."""
    db.upsert_asset("TRX", "Индекс МосБиржи Трона (MOEXTRX)", "CRYPTO")
    db.add_source("TRX", "https://dead.example.org/news/")
    db.add_source("TRX", "https://alive.example.org/rss")
    old = next(s for s in db.list_source_defs() if "dead.example" in s["url"])
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET enabled=0 WHERE id=?", (old["id"],))
    health.report("source", f"disabled:{old['id']}", "страница не существует (404, 9 раз)",
                  severity="info")
    monkeypatch.setattr(health, "URL_FIXES",
                        {"https://dead.example.org/news/": "https://alive.example.org/rss"})
    messages = health.rehabilitate_sources()
    assert any("зависшее уведомление" in m for m in messages)
    assert all(i["key"] != f"disabled:{old['id']}" for i in health.open_incidents())
    assert health.rehabilitate_sources() == [], "повторный запуск ничего не трогает"


@pytest.mark.asyncio
async def test_telegram_conflict_during_deploy_overlap_not_reported(tmp_db, monkeypatch):
    """Conflict сразу после старта — перекрытие экземпляров при деплое: не инцидент.
    Затянувшийся Conflict (минуты спустя) — настоящий двойной запуск: инцидент."""
    import time as _time
    from telegram.error import Conflict
    from unittest.mock import MagicMock, AsyncMock
    from app import main as app_main
    context = MagicMock()
    context.error = Conflict("terminated by other getUpdates request")
    context.application.bot = AsyncMock()
    # внутри окна старта — тишина
    monkeypatch.setattr(app_main, "_PROCESS_STARTED_AT", _time.monotonic())
    await app_main.on_error(None, context)
    assert health.open_incidents() == [], "Conflict при деплое — не инцидент"
    # спустя окно — реальная проблема двух экземпляров, инцидент создаётся
    monkeypatch.setattr(app_main, "_PROCESS_STARTED_AT",
                        _time.monotonic() - app_main.CONFLICT_GRACE_SECONDS - 1)
    await app_main.on_error(None, context)
    assert any(i["key"] == "handler:Conflict" for i in health.open_incidents())


def test_telegram_conflict_incident_closed_on_restart(tmp_db):
    """Conflict в getUpdates — перекрытие двух экземпляров при деплое; после рестарта
    старую запись закрываем вместе с прочими сетевыми сбоями Telegram."""
    health.report("bot", "handler:Conflict: terminated by other getUpdates request",
                  "Conflict: terminated by other getUpdates request")
    messages = health.rehabilitate_sources()
    assert any("Telegram" in m for m in messages)
    assert health.open_incidents() == []


def test_replacement_sources_added_once(tmp_db, monkeypatch):
    db.upsert_asset("HYNIX", "Акции SK Hynix", "SHARE")
    monkeypatch.setattr(health, "REPLACEMENT_SOURCES", {"HYNIX": ["https://news.example.com/en/"],
                                                        "ABSENT": ["https://x.example.com/"]})
    messages = health.rehabilitate_sources()
    assert messages == ["HYNIX: подключён рабочий первоисточник https://news.example.com/en/"]
    assert health.rehabilitate_sources() == []
    assert any(s["url"] == "https://news.example.com/en/" for s in db.list_source_defs())


@pytest.mark.asyncio
async def test_transient_telegram_errors_not_reported(tmp_db):
    from telegram.error import NetworkError
    from unittest.mock import MagicMock, AsyncMock
    from app import main as app_main
    context = MagicMock()
    context.error = NetworkError("Bad Gateway")
    context.application.bot = AsyncMock()
    await app_main.on_error(None, context)
    assert health.open_incidents() == [], "сетевой сбой Telegram — не инцидент"


def test_rehabilitate_closes_stale_incidents(tmp_db, monkeypatch):
    db.upsert_asset("HYNIX", "Акции SK Hynix", "SHARE")
    db.add_source("HYNIX", "https://dead.example.com/news")
    dead = next(s for s in db.list_source_defs() if "dead.example" in s["url"])
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET enabled=0 WHERE id=?", (dead["id"],))
    health.report("source", f"disabled:{dead['id']}", "страница не существует", severity="info")
    health.report("bot", "handler:NetworkError", "Bad Gateway")
    monkeypatch.setattr(health, "REPLACEMENT_SOURCES", {"HYNIX": ["https://alive.example.com/en/"]})
    messages = health.rehabilitate_sources()
    assert any("закрыто уведомление" in m for m in messages)
    assert any("Telegram" in m for m in messages)
    assert health.open_incidents() == []


def test_google_news_incidents_aggregated(tmp_db):
    """70 лент Google News с 503 → один инцидент на хост, не 70 (регресс спама в /health)."""
    db.upsert_asset("A1", "Asset One", "SHARE")
    for i in range(6):
        db.add_source("A1", f"https://news.google.com/rss/search?q=test{i}")
    google = [s for s in db.list_source_defs() if "news.google.com" in s["url"]]
    for src in google:
        for _ in range(health.SOURCE_FAIL_THRESHOLD):
            health.check_source(src, "HTTPStatusError: Server error '503 Service Unavailable'")
    open_now = [i for i in health.open_incidents() if i["component"] == "source"]
    assert len(open_now) == 1 and open_now[0]["key"] == "host:news.google.com"
    assert "недоступны 6 лент" in open_now[0]["message"]
    # Первый же успех любой ленты закрывает общий инцидент.
    health.check_source(google[0], None)
    assert all(i["key"] != "host:news.google.com" for i in health.open_incidents())


def test_rehabilitate_stretches_google_intervals(tmp_db):
    db.upsert_asset("A1", "Asset One", "SHARE")
    db.add_source("A1", "https://news.google.com/rss/search?q=x", refresh_interval=60)
    health.report("source", "source:999", "news.google.com недоступен")
    messages = health.rehabilitate_sources()
    src = next(s for s in db.list_source_defs() if "news.google.com" in s["url"])
    assert src["refresh_interval"] >= 180
    assert any("интервал опроса" in m for m in messages)
    assert all("source:999" != i["key"] for i in health.open_incidents())


def test_resolved_info_never_crashes_alert_formatting(tmp_db):
    """Запись «восстановлено» (без severity) не роняет форматтер тревог (регресс KeyError)."""
    health.report("delivery", "email:daily", "не доставлено")
    info = health.resolve("delivery", "email:daily")
    assert info is None or "severity" not in info
    text = health.format_alert({"component": "delivery", "message": "x", "occurrences": 2})
    assert "delivery" in text


def test_404_disable_needs_long_streak(tmp_db):
    """Кратковременные 404 (сбой CDN) не отключают источник: нужен порог ×3."""
    db.upsert_asset("LITE", "Акции Lumentum Holdings Inc. (LITE)", "SHARE")
    db.add_source("LITE", "https://flaky.example.com/newsroom")
    src = next(s for s in db.list_source_defs() if "flaky" in s["url"])
    for _ in range(health.SOURCE_FAIL_THRESHOLD):
        health.check_source(src, "HTTPStatusError: Client error '404 Not Found'")
    fresh = next(s for s in db.list_source_defs(enabled_only=False) if s["id"] == src["id"])
    assert fresh["enabled"] == 1, "3 сбоев мало для отключения"
    for _ in range(health.SOURCE_FAIL_THRESHOLD * 2):
        health.check_source(src, "HTTPStatusError: Client error '404 Not Found'")
    fresh = next(s for s in db.list_source_defs(enabled_only=False) if s["id"] == src["id"])
    assert fresh["enabled"] == 0, "долгая серия 404 — страница действительно исчезла"


def test_revive_sources_reenabled(tmp_db, monkeypatch):
    db.upsert_asset("LITE", "Акции Lumentum Holdings Inc. (LITE)", "SHARE")
    db.add_source("LITE", "https://revive.example.com/newsroom")
    src = next(s for s in db.list_source_defs() if "revive" in s["url"])
    with db.connect() as conn:
        conn.execute("UPDATE source_defs SET enabled=0, fail_streak=3 WHERE id=?", (src["id"],))
    monkeypatch.setattr(health, "REVIVE_SOURCES", ["https://revive.example.com/newsroom"])
    messages = health.rehabilitate_sources()
    assert any("возвращён в мониторинг" in m for m in messages)
    fresh = next(s for s in db.list_source_defs(enabled_only=False) if s["id"] == src["id"])
    assert fresh["enabled"] == 1
    assert health.rehabilitate_sources() == []


def test_nasdaq_press_release_pages_retired(tmp_db):
    """Страницы nasdaq.com/…/press-releases по акциям — каркас без новостей: при старте
    выключаются и отписываются, их инциденты закрываются; ETF-страницы не трогаются."""
    db.upsert_asset("TSLA", "Акции Tesla, Inc. (TSLA)", "SHARE")
    db.upsert_asset("NASD", "Invesco QQQ ETF Trust Unit Series 1 (QQQ)", "ETF")
    stock = "https://www.nasdaq.com/market-activity/stocks/tsla/press-releases"
    etf = "https://www.nasdaq.com/market-activity/etf/qqq/press-releases"
    db.add_source("TSLA", stock)
    db.add_source("TSLA", "https://www.nasdaq.com/market-activity/stocks/tsla")
    db.add_source("NASD", etf)
    sid = next(s["id"] for s in db.list_source_defs() if s["url"] == stock)
    health.report("source", f"source:{sid}", "недоступен 5 проверок подряд")
    messages = health.rehabilitate_sources()
    assert any("выведено из мониторинга 2 страниц" in m for m in messages)
    assert db.get_asset("TSLA")["sources"] == []
    assert stock not in {s["url"] for s in db.list_source_defs(enabled_only=True)}
    assert etf in {s["url"] for s in db.get_asset("NASD")["sources"]}
    assert not [i for i in health.open_incidents() if i["key"] == f"source:{sid}"]
    assert health.rehabilitate_sources() == [], "повторный запуск ничего не трогает"


def test_us_stock_url_fixes_match_methodology():
    """Методология уже содержит рабочие адреса: старые (пустые/403) в ней не встречаются,
    новые — встречаются, иначе синхронизация возвращала бы старые подписки."""
    from app import assets_sync
    from tests.test_assets_sync import MD
    md_urls = {u for a in assets_sync.parse_underlyings_md(MD) for u in a["sources"]}
    us = {old: new for old, new in health.URL_FIXES.items()
          if any(d in old for d in ("aboutamazon", "uber.com", "bostonscientific", "chipotle", "spacex",
                                    "supermicro", "robinhood", "coinbase", "lumentum", "doordash",
                                    "applovin", "carvana"))}
    assert len(us) == 14
    for old, new in us.items():
        assert old not in md_urls, old
        assert new in md_urls, new
    assert not any("nasdaq.com/market-activity/stocks/" in u for u in md_urls)


def test_existing_db_migrates_to_new_us_sources(tmp_db):
    """Старая база (страницы nasdaq, пустые IR-страницы): после старта у акций — рабочие
    ленты, подписки на заменённые адреса перенесены, второй старт ничего не меняет."""
    from app import assets_sync
    from tests.test_assets_sync import MD
    db.upsert_asset("COIN", "Акции Coinbase Global, Inc. (COIN)", "SHARE")
    for url in ("https://investor.coinbase.com/", "https://investor.coinbase.com/news/default.aspx",
                "https://www.nasdaq.com/market-activity/stocks/coin/press-releases"):
        db.add_source("COIN", url)
    assets_sync.sync_from_md(MD)
    assets_sync.ensure_backup_sources()
    health.rehabilitate_sources()
    urls = {s["url"] for s in db.get_asset("COIN")["sources"]}
    assert f"https://investor.coinbase.com/{health._Q4_RSS}" in urls
    assert not any("nasdaq.com" in u or u.endswith("default.aspx") or u == "https://investor.coinbase.com/"
                   for u in urls), urls
    assert db.get_asset("COIN")["underlying_ticker"] == "COIN"
    assert assets_sync.sync_from_md(MD)["sources_added"] == 0
    assert health.rehabilitate_sources() == []
