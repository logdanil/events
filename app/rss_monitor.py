"""Опрос RSS/Atom-источников. Каждый источник скачивается один раз за цикл,
все источники — параллельно. Возвращает унифицированные публикации."""
from __future__ import annotations

import asyncio
import calendar
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx

from app import db, fetcher, health
from app.config import settings

log = logging.getLogger(__name__)

MAX_ENTRIES_PER_FEED = 50
TEXT_LIMIT = 1200

# Заголовок SEC EDGAR: "8-K - COMPANY NAME (0001770561) (Filer)"
SEC_TITLE_RE = re.compile(
    r"^(?P<form>[0-9A-Z][0-9A-Z\-/]{0,9})\s*-\s*(?P<filer>.+?)\s*\((?P<cik>\d{7,10})\)")


def _entry_published(entry) -> tuple[str | None, datetime | None]:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            dt = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            return dt.isoformat(), dt
    raw = entry.get("published") or entry.get("updated")
    return (raw, None) if raw else (None, None)


def parse_feed(content: bytes | str, source: dict) -> list[dict]:
    """Разбирает содержимое фида в публикации (синхронно, вызывается в thread)."""
    feed = feedparser.parse(content)
    if not feed.entries and isinstance(content, (bytes, str)):
        # HTML под RSS-адресом (bnbchain.org/blog/rss.xml) — отдаём HTML-парсеру.
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
        if "<html" in text[:400].lower():
            from app.html_monitor import parse_links
            return parse_links(text, source)
    publications = []
    for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
        url = entry.get("link") or ""
        title = (entry.get("title") or "").strip()
        if not url or not title:
            continue
        summary = (entry.get("summary") or entry.get("description") or "")
        published_at, published_dt = _entry_published(entry)
        pub = {
            "url": url,
            "title": title,
            "text": summary[:TEXT_LIMIT],
            "published_at": published_at,
            "published_dt": published_dt,
            "source": source["name"],
            "source_id": source["id"],
            "source_type": source["type"],
            "subscribers": source.get("subscribers", []),
        }
        # SEC-фиды: сопоставление идёт по filer (имя + CIK), а не по тексту заголовка.
        if source["type"] == "SEC":
            m = SEC_TITLE_RE.match(title)
            if m:
                pub["form_type"] = m.group("form")
                pub["sec_filer"] = m.group("filer").strip()
                pub["sec_cik"] = m.group("cik")
            else:
                # Персональная лента эмитента (…getcompany&CIK=…): filer в заголовке
                # отсутствует, CIK берём из URL источника — матчинг по CIK детерминирован.
                cik_m = re.search(r"[?&]CIK=0*(\d+)", source["url"])
                if cik_m:
                    pub["sec_cik"] = cik_m.group(1)
                    form_m = re.match(r"^\s*([0-9A-Z][0-9A-Z\-/]{0,9}?)\s+-\s", title)
                    pub["form_type"] = form_m.group(1) if form_m else title[:10].strip()
        publications.append(pub)
    return publications


async def fetch_source(client: httpx.AsyncClient, source: dict) -> list[dict]:
    try:
        content = await fetcher.fetch(client, source)
        publications = await asyncio.to_thread(parse_feed, content, source)
        db.mark_source_checked(source["id"])
        alert = health.check_source(source, None)
        if alert:
            health.pending_alerts.append(alert)
        return publications
    except Exception as exc:
        db.mark_source_checked(source["id"], error=f"{type(exc).__name__}: {exc}"[:300])
        alert = health.check_source(source, f"{type(exc).__name__}: {exc}")
        if alert:
            health.pending_alerts.append(alert)
        log.warning("RSS-источник %s недоступен: %s: %s", source["name"], type(exc).__name__, exc)
        return []


async def fetch_all(client: httpx.AsyncClient, sources: list[dict]) -> list[dict]:
    """Скачивает все RSS-источники параллельно."""
    if not sources:
        return []
    results = await asyncio.gather(*(fetch_source(client, s) for s in sources))
    publications = [p for batch in results for p in batch]
    log.info("RSS: %d источников → %d публикаций", len(sources), len(publications))
    return publications
