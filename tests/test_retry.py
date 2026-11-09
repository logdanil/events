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


def test_impersonated_reports_each_browser_status(monkeypatch):
    from unittest.mock import Mock

    from curl_cffi import requests
    from app import fetcher

    responses = [Mock(status_code=403), Mock(status_code=429), Mock(status_code=403)]
    monkeypatch.setattr(requests, "get", Mock(side_effect=responses))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        fetcher._fetch_impersonated("https://news.samsung.com/global/feed")
    assert caught.value.response.status_code == 403
    for browser, status in zip(fetcher.IMPERSONATE_CHAIN, (403, 429, 403)):
        assert f"{browser}=HTTP {status}" in str(caught.value)
    for response in responses:
        response.close.assert_called_once()


def test_impersonated_transport_errors_are_not_http_statuses(monkeypatch):
    from unittest.mock import Mock

    from curl_cffi import requests
    from curl_cffi.requests.exceptions import RequestException
    from app import fetcher

    error = RequestException("connection failed")
    monkeypatch.setattr(requests, "get", Mock(side_effect=error))
    with pytest.raises(httpx.RequestError) as caught:
        fetcher._fetch_impersonated("https://global.toyota/en/newsroom/")
    assert caught.value.__cause__ is error
    for browser in fetcher.IMPERSONATE_CHAIN:
        assert f"{browser}=RequestException" in str(caught.value)


def test_impersonated_final_transport_failure_keeps_prior_http_diagnostics(monkeypatch):
    from unittest.mock import Mock

    from curl_cffi import requests
    from curl_cffi.requests.exceptions import RequestException
    from app import fetcher

    blocked = Mock(status_code=403)
    error = RequestException("connection failed")
    monkeypatch.setattr(requests, "get", Mock(side_effect=[blocked, error, error]))
    with pytest.raises(httpx.RequestError) as caught:
        fetcher._fetch_impersonated("https://example.com/feed")
    assert caught.value.__cause__ is error
    assert "chrome=HTTP 403" in str(caught.value)
    assert "safari=RequestException" in str(caught.value)
    assert "firefox135=RequestException" in str(caught.value)
    blocked.close.assert_called_once()


def test_impersonated_retries_stream_failure_and_closes_responses(monkeypatch):
    from unittest.mock import Mock

    from curl_cffi import requests
    from curl_cffi.requests.exceptions import RequestException
    from app import fetcher

    broken = Mock(status_code=200)
    broken.iter_content.side_effect = RequestException("stream interrupted")
    working = Mock(status_code=200)
    working.iter_content.return_value = iter([b"feed"])
    get = Mock(side_effect=[broken, working])
    monkeypatch.setattr(requests, "get", get)
    assert fetcher._fetch_impersonated("https://example.com/feed") == b"feed"
    assert get.call_count == 2
    broken.close.assert_called_once()
    working.close.assert_called_once()


def test_impersonated_does_not_hide_unexpected_errors(monkeypatch):
    from unittest.mock import Mock

    from curl_cffi import requests
    from app import fetcher

    monkeypatch.setattr(requests, "get", Mock(side_effect=ValueError("invalid configuration")))
    with pytest.raises(ValueError, match="invalid configuration"):
        fetcher._fetch_impersonated("https://example.com/feed")
