"""Скачивание страниц источников с обходом антибот-защиты.

Первый заход — httpx (быстрый, общий пул соединений). Если сайт отвечает
403/429 (антибот по TLS-отпечатку: Akamai, Cloudflare…), повтор через
curl_cffi с имитацией Chrome — это открывает Sony, TSMC, CME, Invesco и др.
SEC всегда ходит через httpx с контактным User-Agent (их требование).
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def plain_text(text: str | None) -> str:
    """HTML-описание из ленты → обычный текст. Описание Google News — это ссылка
    с base64-адресом: без очистки она съедает токены модели и даёт ложные
    совпадения ключевых слов («adr» внутри base64)."""
    if not text:
        return ""
    if "<" in text or "&" in text:
        text = html.unescape(_TAG_RE.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip()

BLOCK_CODES = {403, 429, 406}

# Одновременных загрузок: без лимита 290 источников исчерпывают пул соединений
# httpx (PoolTimeout) и пул потоков curl_cffi. SEC дополнительно ограничен —
# их правила требуют не более ~10 запросов/сек с одного клиента.
MAX_CONCURRENT_FETCHES = 24
MAX_CONCURRENT_SEC = 6
# Google News банит за частые запросы (503 на все ленты): ходим к нему по двое
# и с паузой-разбросом, чтобы 70+ лент не били одновременно.
MAX_CONCURRENT_GOOGLE = 2
# Одновременных разборов страниц/фидов в потоках: каждый разбор держит в памяти
# исходный текст и дерево; без лимита пул потоков разбирал до 32 страниц разом.
MAX_CONCURRENT_PARSES = 4
# Больше этого объёма с источника не читаем: лента новостей или IR-страница
# укладываются в сотни килобайт, а многомегабайтные ответы (полные архивы,
# бесконечные ленты) только раздувают память парсера.
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
_semaphore: asyncio.Semaphore | None = None
_sec_semaphore: asyncio.Semaphore | None = None
_google_semaphore: asyncio.Semaphore | None = None
_parse_semaphore: asyncio.Semaphore | None = None


def _sems() -> tuple[asyncio.Semaphore, asyncio.Semaphore, asyncio.Semaphore]:
    # Создаются лениво, внутри работающего event loop.
    global _semaphore, _sec_semaphore, _google_semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        _sec_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEC)
        _google_semaphore = asyncio.Semaphore(MAX_CONCURRENT_GOOGLE)
    return _semaphore, _sec_semaphore, _google_semaphore


async def parse_in_thread(func, *args):
    """Выполняет синхронный разбор в потоке, не более MAX_CONCURRENT_PARSES одновременно."""
    global _parse_semaphore
    if _parse_semaphore is None:
        _parse_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PARSES)
    async with _parse_semaphore:
        return await asyncio.to_thread(func, *args)
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
    from curl_cffi.requests.exceptions import RequestException

    last_status = None
    last_error = None
    attempts = []
    for browser in IMPERSONATE_CHAIN:
        response = None
        try:
            response = curl_requests.get(url, impersonate=browser, timeout=30,
                                         allow_redirects=True, stream=True)
            if response.status_code < 400:
                chunks, size = [], 0
                for chunk in response.iter_content():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_RESPONSE_BYTES:
                        break
                if browser != IMPERSONATE_CHAIN[0]:
                    log.info("Источник %s открылся под %s", url, browser)
                return b"".join(chunks)[:MAX_RESPONSE_BYTES]
            last_status = response.status_code
            last_error = None
            attempts.append(f"{browser}=HTTP {last_status}")
        except RequestException as exc:
            last_status = None
            last_error = exc
            attempts.append(f"{browser}={type(exc).__name__}")
            log.warning("Источник %s, TLS %s: %s: %s",
                        url, browser, type(exc).__name__, exc)
        finally:
            if response is not None:
                response.close()
    request = httpx.Request("GET", url)
    details = f"TLS: {', '.join(attempts)}; URL: {url}"
    if last_status is None:
        raise httpx.RequestError(
            f"{details}; {last_error}", request=request) from last_error
    raise httpx.HTTPStatusError(
        f"HTTP {last_status}; {details}",
        request=request,
        response=httpx.Response(last_status, request=request)) from last_error


async def fetch(client: httpx.AsyncClient, source: dict) -> bytes:
    """Содержимое страницы источника. Бросает исключение, если недоступно
    обоими способами (учёт серий сбоев остаётся на вызывающей стороне)."""
    sem, sec_sem, google_sem = _sems()
    if "sec.gov" in source["url"]:
        async with sec_sem:
            return await _fetch_inner(client, source)
    if "news.google.com" in source["url"]:
        async with google_sem:
            await asyncio.sleep(random.uniform(0.5, 2.0))
            return await _fetch_inner(client, source)
    async with sem:
        return await _fetch_inner(client, source)


async def _read_limited(client: httpx.AsyncClient, source: dict) -> bytes:
    """GET с потоковым чтением: тело обрезается по MAX_RESPONSE_BYTES, не попадая
    в память целиком."""
    async with client.stream("GET", source["url"], headers=request_headers(source),
                             follow_redirects=True, timeout=25) as response:
        response.raise_for_status()
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_RESPONSE_BYTES:
                log.info("Источник %s больше %d КБ — читаю только начало",
                         source.get("name"), MAX_RESPONSE_BYTES // 1024)
                break
        return b"".join(chunks)[:MAX_RESPONSE_BYTES]


async def _fetch_inner(client: httpx.AsyncClient, source: dict) -> bytes:
    try:
        return await _read_limited(client, source)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in BLOCK_CODES or "sec.gov" in source["url"] \
                or "news.google.com" in source["url"]:
            # Google/SEC режут по IP и частоте — имитация браузера не поможет,
            # а лишние повторы только продлевают бан.
            raise
        log.info("Источник %s отдал %s — пробую с TLS-отпечатком браузера",
                 source["name"], exc.response.status_code)
        return await asyncio.to_thread(_fetch_impersonated, source["url"])
    except httpx.TimeoutException:
        # Медленные/антибот-сайты (iShares, HKEX) часто отвечают браузеру, но душат
        # обычный клиент — повторяем с браузерным TLS-отпечатком и большим таймаутом.
        if "sec.gov" in source["url"] or "news.google.com" in source["url"]:
            raise
        log.info("Источник %s не ответил вовремя — пробую с TLS-отпечатком браузера",
                 source["name"])
        return await asyncio.to_thread(_fetch_impersonated, source["url"])
