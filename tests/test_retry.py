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
        def raise_for_status(self): pass
        async def aiter_bytes(self):
            yield b"ok"

    class FakeStream:
        async def __aenter__(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            return FakeResponse()
        async def __aexit__(self, *exc):
            nonlocal active
            active -= 1
            return False

    class FakeClient:
        def stream(self, method, url, **kw): return FakeStream()

    client = FakeClient()
    sources = [{"id": i, "name": f"s{i}", "type": "IR", "url": f"https://x{i}.example.com/"}
               for i in range(150)]
    results = await asyncio.gather(*(fetcher.fetch(client, s) for s in sources))
    assert all(r == b"ok" for r in results)
    assert peak <= fetcher.MAX_CONCURRENT_FETCHES, f"пик одновременности {peak}"


@pytest.mark.asyncio
async def test_fetch_truncates_huge_response(monkeypatch):
    """Ответ больше MAX_RESPONSE_BYTES читается только до лимита — память не раздувается."""
    import asyncio
    from app import fetcher
    fetcher._semaphore = fetcher._sec_semaphore = None
    monkeypatch.setattr(fetcher, "MAX_RESPONSE_BYTES", 1000)
    served = {"chunks": 0}

    class FakeResponse:
        def raise_for_status(self): pass
        async def aiter_bytes(self):
            for _ in range(100):
                served["chunks"] += 1
                yield b"x" * 100
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    class FakeClient:
        def stream(self, method, url, **kw): return FakeResponse()

    body = await fetcher.fetch(FakeClient(), {"id": 1, "name": "big", "type": "IR",
                                              "url": "https://big.example.com/"})
    assert len(body) == 1000
    assert served["chunks"] == 10  # остальные 90 чанков не запрашивались
