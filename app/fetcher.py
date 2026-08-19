"""Скачивание страниц источников с обходом антибот-защиты.

Первый заход — httpx (быстрый, общий пул соединений). Если сайт отвечает
403/429 (антибот по TLS-отпечатку: Akamai, Cloudflare…), повтор через
curl_cffi с имитацией Chrome — это открывает Sony, TSMC, CME, Invesco и др.
SEC всегда ходит через httpx с контактным User-Agent (их требование).
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

BLOCK_CODES = {403, 429, 406}


def request_headers(source: dict) -> dict:
    """SEC требует контактный User-Agent; остальные сайты лучше отвечают браузерному."""
    if source.get("type") == "SEC" or "sec.gov" in source.get("url", ""):
        return {"User-Agent": settings.user_agent, "Accept": "*/*"}
    return {"User-Agent": settings.browser_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}


def _fetch_impersonated(url: str) -> bytes:
    """Синхронный запрос с TLS-отпечатком Chrome (выполняется в отдельном потоке)."""
    from curl_cffi import requests as curl_requests
    response = curl_requests.get(url, impersonate="chrome", timeout=25,
                                 allow_redirects=True)
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Client error '{response.status_code}' for url '{url}' (impersonated)",
            request=httpx.Request("GET", url),
            response=httpx.Response(response.status_code, request=httpx.Request("GET", url)))
    return response.content


async def fetch(client: httpx.AsyncClient, source: dict) -> bytes:
    """Содержимое страницы источника. Бросает исключение, если недоступно
    обоими способами (учёт серий сбоев остаётся на вызывающей стороне)."""
    try:
        response = await client.get(source["url"], headers=request_headers(source),
                                    follow_redirects=True, timeout=20)
        response.raise_for_status()
        return response.content
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in BLOCK_CODES or "sec.gov" in source["url"]:
            raise
        log.info("Источник %s отдал %s — пробую с TLS-отпечатком Chrome",
                 source["name"], exc.response.status_code)
        return await asyncio.to_thread(_fetch_impersonated, source["url"])
