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
# Разные антибот-системы режут разные браузеры: LME пропускает Safari/Firefox, но не Chrome.
IMPERSONATE_CHAIN = ("chrome", "safari", "firefox135")


def request_headers(source: dict) -> dict:
    """SEC требует контактный User-Agent; остальные сайты лучше отвечают браузерному."""
    if source.get("type") == "SEC" or "sec.gov" in source.get("url", ""):
        return {"User-Agent": settings.user_agent, "Accept": "*/*"}
    return {"User-Agent": settings.browser_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}


def _fetch_impersonated(url: str) -> bytes:
    """Синхронный запрос с TLS-отпечатком браузера; перебирает несколько браузеров,
    потому что защита у каждого сайта пропускает разные (выполняется в отдельном потоке)."""
    from curl_cffi import requests as curl_requests
    last_status = None
    for browser in IMPERSONATE_CHAIN:
        try:
            response = curl_requests.get(url, impersonate=browser, timeout=30,
                                         allow_redirects=True)
        except Exception:
            continue
        if response.status_code < 400:
            if browser != IMPERSONATE_CHAIN[0]:
                log.info("Источник %s открылся под %s", url, browser)
            return response.content
        last_status = response.status_code
    raise httpx.HTTPStatusError(
        f"Client error '{last_status or 'timeout'}' for url '{url}' (все отпечатки: {', '.join(IMPERSONATE_CHAIN)})",
        request=httpx.Request("GET", url),
        response=httpx.Response(last_status or 599, request=httpx.Request("GET", url)))


async def fetch(client: httpx.AsyncClient, source: dict) -> bytes:
    """Содержимое страницы источника. Бросает исключение, если недоступно
    обоими способами (учёт серий сбоев остаётся на вызывающей стороне)."""
    try:
        response = await client.get(source["url"], headers=request_headers(source),
                                    follow_redirects=True, timeout=25)
        response.raise_for_status()
        return response.content
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in BLOCK_CODES or "sec.gov" in source["url"]:
            raise
        log.info("Источник %s отдал %s — пробую с TLS-отпечатком браузера",
                 source["name"], exc.response.status_code)
        return await asyncio.to_thread(_fetch_impersonated, source["url"])
    except httpx.TimeoutException:
        # Медленные/антибот-сайты (iShares, HKEX) часто отвечают браузеру, но душат
        # обычный клиент — повторяем с браузерным TLS-отпечатком и большим таймаутом.
        if "sec.gov" in source["url"]:
            raise
        log.info("Источник %s не ответил вовремя — пробую с TLS-отпечатком браузера",
                 source["name"])
        return await asyncio.to_thread(_fetch_impersonated, source["url"])
