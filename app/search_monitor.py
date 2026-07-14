"""Третий уровень каскада: резервный поиск через Tavily.
Выполняется не чаще раза в сутки (настройка tavily_last_run в БД) либо принудительно
по команде /fullscan. При обычных сканированиях Tavily не используется."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from tavily import TavilyClient

from app import db
from app.config import settings
from app.retry import with_retries

log = logging.getLogger(__name__)

TEXT_LIMIT = 1200
LAST_RUN_KEY = "tavily_last_run"


def is_due() -> bool:
    last = db.get_setting(LAST_RUN_KEY, "")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= 24 * 3600


def mark_run() -> None:
    db.set_setting(LAST_RUN_KEY, datetime.now(timezone.utc).isoformat())


def _query(asset: dict) -> str:
    aliases = " ".join(a for a in asset.get("aliases", [])[:2])
    return f'"{asset["name"]}" {aliases} corporate action OR announcement OR notice'[:400]


class SearchMonitor:
    def __init__(self) -> None:
        self.client = TavilyClient(api_key=settings.tavily_api_key)

    async def _search_asset(self, asset: dict) -> list[dict]:
        try:
            response = await with_retries(
                lambda: asyncio.to_thread(
                    self.client.search, query=_query(asset), topic="news",
                    search_depth="basic", max_results=settings.max_search_results,
                    days=settings.lookback_days),
                what=f"Tavily fallback [{asset['code']}]")
        except Exception as exc:
            log.warning("Tavily fallback для %s не удался: %s: %s", asset["code"], type(exc).__name__, exc)
            return []
        publications = []
        for r in response.get("results", []):
            if not r.get("url"):
                continue
            publications.append({
                "url": r["url"],
                "title": (r.get("title") or "").strip(),
                "text": (r.get("content") or "")[:TEXT_LIMIT],
                "published_at": r.get("published_date"),
                "published_dt": None,
                "source": "Tavily",
                "source_id": None,
                "source_type": "SEARCH",
                # результат искали по конкретному активу — атрибуция ему
                "subscribers": [asset["code"]],
            })
        return publications

    async def fetch_all(self, assets: list[dict]) -> list[dict]:
        """По одному компактному запросу на актив, конкурентно (не более 5 одновременно)."""
        semaphore = asyncio.Semaphore(5)

        async def guarded(asset):
            async with semaphore:
                return await self._search_asset(asset)

        results = await asyncio.gather(*(guarded(a) for a in assets))
        publications = [p for batch in results for p in batch]
        log.info("Tavily fallback: %d активов → %d публикаций", len(assets), len(publications))
        return publications
