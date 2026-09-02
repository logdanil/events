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


@pytest.mark.asyncio
async def test_fetch_concurrency_is_bounded():
    """290 источников не должны исчерпывать пул соединений (регресс PoolTimeout):
    одновременных загрузок не больше MAX_CONCURRENT_FETCHES."""
    import asyncio
    from app import fetcher
    fetcher._semaphore = fetcher._sec_semaphore = None  # свежие семафоры
    active, peak = 0, 0

    class FakeResponse:
        content = b"ok"
        def raise_for_status(self): pass

    class FakeClient:
        async def get(self, url, **kw):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return FakeResponse()

    client = FakeClient()
    sources = [{"id": i, "name": f"s{i}", "type": "IR", "url": f"https://x{i}.example.com/"}
               for i in range(150)]
    results = await asyncio.gather(*(fetcher.fetch(client, s) for s in sources))
    assert all(r == b"ok" for r in results)
    assert peak <= fetcher.MAX_CONCURRENT_FETCHES, f"пик одновременности {peak}"
