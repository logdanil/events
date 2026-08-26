"""Второй уровень каскада: источники без RSS. Страница скачивается один раз,
из неё извлекаются заголовки-ссылки (пресс-релизы, новости IR-разделов).
Дата публикации обычно недоступна — от повторной обработки защищает кэш processed_articles."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app import db, fetcher, health
from app.config import settings

log = logging.getLogger(__name__)

MAX_LINKS_PER_PAGE = 30
MIN_TITLE_LEN = 25


def parse_json_articles(text: str, source: dict) -> list[dict]:
    """JSON-API объявлений (Binance CMS и подобные): рекурсивно собирает объекты
    с полем title; ссылка — url/link, либо код анонса Binance, либо страница источника."""
    import json
    from datetime import datetime, timezone
    try:
        data = json.loads(text)
    except ValueError:
        return []
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            title = node.get("title")
            if isinstance(title, str) and len(title.strip()) >= MIN_TITLE_LEN:
                url = node.get("url") or node.get("link")
                if not url and node.get("code") and "binance.com" in source["url"]:
                    url = f"https://www.binance.com/en/support/announcement/{node['code']}"
                if not url:
                    url = f"{source['url']}#{node.get('id') or node.get('code') or len(found)}"
                published_dt = None
                stamp = node.get("releaseDate") or node.get("publishDate") or node.get("createTime")
                if isinstance(stamp, (int, float)) and stamp > 1e9:
                    published_dt = datetime.fromtimestamp(stamp / (1000 if stamp > 1e11 else 1), tz=timezone.utc)
                found.append({"url": url, "title": " ".join(title.split()), "text": str(node.get("body") or node.get("summary") or "")[:1200],
                              "published_at": published_dt.isoformat() if published_dt else None,
                              "published_dt": published_dt, "source": source["name"],
                              "source_id": source["id"], "source_type": source["type"],
                              "subscribers": source.get("subscribers", [])})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    return found[:MAX_LINKS_PER_PAGE]


def parse_links(html: str, source: dict) -> list[dict]:
    """Извлекает содержательные ссылки со страницы (синхронно, вызывается в thread)."""
    head = html.lstrip()[:400].lower()
    if head[:1] in "{[":
        return parse_json_articles(html, source)
    if "<rss" in head or "<feed" in head or "<rdf:rdf" in head:
        # RSS/Atom под HTML-адресом (например, moex.com/export/news.aspx) — отдаём фид-парсеру.
        from app.rss_monitor import parse_feed
        return parse_feed(html, source)
    soup = BeautifulSoup(html, "html.parser")
    publications = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        title = " ".join(anchor.get_text(" ", strip=True).split())
        href = anchor["href"].strip()
        if len(title) < MIN_TITLE_LEN or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(source["url"], href)
        if url in seen or url.rstrip("/") == source["url"].rstrip("/"):
            continue
        seen.add(url)
        publications.append({
            "url": url,
            "title": title[:300],
            "text": "",
            "published_at": None,
            "published_dt": None,
            "source": source["name"],
            "source_id": source["id"],
            "source_type": source["type"],
            "subscribers": source.get("subscribers", []),
        })
        if len(publications) >= MAX_LINKS_PER_PAGE:
            break
    return publications


async def fetch_source(client: httpx.AsyncClient, source: dict) -> list[dict]:
    try:
        content = await fetcher.fetch(client, source)
        publications = await asyncio.to_thread(
            parse_links, content.decode("utf-8", errors="replace"), source)
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
        log.warning("HTML-источник %s недоступен: %s: %s", source["name"], type(exc).__name__, exc)
        return []


async def fetch_all(client: httpx.AsyncClient, sources: list[dict]) -> list[dict]:
    """Скачивает все HTML-источники параллельно."""
    if not sources:
        return []
    results = await asyncio.gather(*(fetch_source(client, s) for s in sources))
    publications = [p for batch in results for p in batch]
    log.info("HTML: %d источников → %d публикаций", len(sources), len(publications))
    return publications
