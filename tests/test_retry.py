from __future__ import annotations

import httpx
import pytest

from app.retry import with_retries


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example.com")
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(status, request=request))


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429)
        return "ok"

    assert await with_retries(flaky, what="test", base_delay=0.01) == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retries_on_5xx_and_gives_up():
    calls = {"n": 0}

    async def always_broken():
        calls["n"] += 1
        raise _http_error(503)

    with pytest.raises(httpx.HTTPStatusError):
        await with_retries(always_broken, what="test", attempts=3, base_delay=0.01)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_no_retry_on_permanent_error():
    calls = {"n": 0}

    async def bad_request():
        calls["n"] += 1
        raise _http_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        await with_retries(bad_request, what="test", base_delay=0.01)
    assert calls["n"] == 1
