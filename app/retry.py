from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

import httpx
import telegram.error
from tavily import errors as tavily_errors

log = logging.getLogger(__name__)

T = TypeVar("T")


def _retry_delay_hint(exc: Exception) -> float | None:
    """Возвращает None, если ошибка не временная, иначе рекомендованную задержку (0 = по backoff)."""
    # Tavily: лимит запросов и таймауты; 5xx приходят как httpx.HTTPStatusError.
    if isinstance(exc, tavily_errors.TavilyKeylessLimitError):
        return float(exc.retry_after_seconds or 0)
    if isinstance(exc, (tavily_errors.UsageLimitExceededError, tavily_errors.TimeoutError)):
        return 0.0
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            # Лимиты частоты (Gemini RPM и т.п.) считаются на минуту:
            # повтор через секунду упрётся в тот же лимит.
            return 15.0
        return 0.0 if code >= 500 else None
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return 0.0
    # Telegram: RetryAfter содержит точную задержку, TimedOut/NetworkError — временные.
    if isinstance(exc, telegram.error.RetryAfter):
        delta = exc.retry_after
        return float(getattr(delta, "total_seconds", lambda: delta)())
    if isinstance(exc, (telegram.error.TimedOut, telegram.error.NetworkError)):
        return 0.0
    return None


async def with_retries(
    func: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> T:
    """Выполняет func с повторами при 429 и временных 5xx: экспоненциальная задержка с джиттером."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except Exception as exc:
            hint = _retry_delay_hint(exc)
            if hint is None or attempt == attempts:
                raise
            delay = min(max_delay, max(hint, base_delay * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)))
            log.warning("%s: временная ошибка (%s: %s), повтор %d/%d через %.1f c",
                        what, type(exc).__name__, exc, attempt, attempts - 1, delay)
            last_exc = exc
            await asyncio.sleep(delay)
    raise last_exc  # pragma: no cover - недостижимо, оставлено для типизации
