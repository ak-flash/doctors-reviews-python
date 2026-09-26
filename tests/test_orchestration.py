import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import main
from app.cache import AsyncTTLCache
from app.limiter import FixedWindowRateLimiter
from collectors.base import CollectorResult, Platform, Review, SourceBlockedError
from collectors.http_client import HTTPClient


async def noop_validator(url):
    return None


@pytest.fixture(autouse=True)
def browser_fallback(monkeypatch):
    fallback = AsyncMock(side_effect=SourceBlockedError("Browser verification did not complete"))
    monkeypatch.setattr("collectors.sberzdorovie.collect_with_browser", fallback)
    monkeypatch.setattr("collectors.prodoctorov.collect_with_browser", fallback)
    return fallback


@pytest.fixture(autouse=True)
def reset_main(monkeypatch):
    monkeypatch.setattr(main, "API_AUTH_ENABLED", False)
    monkeypatch.setattr(main, "API_KEY", "")
    monkeypatch.setattr(main, "STARTUP_WARMUP", False)
    main.app.state.cache = AsyncTTLCache(60, 10)
    main.app.state.blocked_cache = AsyncTTLCache(60, 10)
    main.app.state.semaphore = asyncio.Semaphore(1)
    main.app.state.rate_limiter = FixedWindowRateLimiter(100, 60)


@pytest.fixture
def install_http(monkeypatch):
    def install(handler):
        handler = Mock(side_effect=handler)
        transport = httpx.MockTransport(handler)

        def factory(domains):
            return HTTPClient(domains, retries=0, client=httpx.AsyncClient(transport=transport), url_validator=noop_validator)

        monkeypatch.setattr(main, "HTTPClient", factory)
        return handler

    return install


@pytest.mark.asyncio
async def test_failed_browser_fallback_returns_source_error(install_http, browser_fallback):
    install_http(lambda request: httpx.Response(403, text="blocked"))

    result = await main.fetch("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE)

    assert result.status_code == 503
    browser_fallback.assert_awaited_once_with("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE, False)


@pytest.mark.asyncio
async def test_http_timeout_returns_collection_error(install_http):
    def handler(request):
        raise httpx.ReadTimeout("timeout")

    install_http(handler)
    result = await main.fetch("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE)

    assert result.status_code == 504


@pytest.mark.asyncio
async def test_repeated_fetch_uses_cache(install_http):
    calls = 0
    payload = '{"props":{"pageProps":{"preloadedState":{"doctorPage":{"doctor":{"reviewsForSeo":[{"text":"Good"}]}}}}}}'

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=f'<title>Doctor</title><script id="__NEXT_DATA__">{payload}</script>')

    install_http(handler)
    first = await main.fetch("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE)
    second = await main.fetch("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE)

    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_normal_pages_use_http_collectors(install_http):
    sber_payload = '{"props":{"pageProps":{"preloadedState":{"doctorPage":{"doctor":{"reviewsForSeo":[{"name":"Ann","text":"Good","rating":{"value":5}}]}}}}}}'
    prodoctorov_html = '<title>Doctor</title><div class="b-review-card"><div itemprop="reviewBody" data="r1"></div><a class="b-review-card__author-link">Ann</a><div itemprop="datePublished" content="2025-01-02">2 Jan</div><div class="b-review-card__comment">Text</div><meta itemprop="ratingValue" content="5"></div>'
    responses = {
        "docdoc.ru": f'<title>Sber</title><script id="__NEXT_DATA__">{sber_payload}</script>',
        "prodoctorov.ru": prodoctorov_html,
    }
    install_http(lambda request: httpx.Response(200, text=responses[request.url.host]))

    sber = await main.fetch("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE)
    prodoctorov = await main.fetch("https://prodoctorov.ru/doctor/a", Platform.PRODOCTOROV)

    assert sber["title"] == "Sber"
    assert sber["reviews"][0]["message"] == "Good"
    assert prodoctorov["reviews"][0]["id"] == "r1"
    assert prodoctorov["reviews"][0]["rating"] == 50


@pytest.mark.asyncio
async def test_blocked_result_uses_short_cache(install_http, browser_fallback):
    main.app.state.blocked_cache = AsyncTTLCache(60, 10)
    handler = install_http(lambda request: httpx.Response(403, text="blocked"))

    first = await main.fetch("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE)
    second = await main.fetch("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE)

    assert first.status_code == 503
    assert second.status_code == 503
    assert handler.call_count == 1
    browser_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_result_reviews_are_limited(monkeypatch, install_http):
    monkeypatch.setattr(main, "MAX_RESULT_REVIEWS", 1)
    payload = '{"props":{"pageProps":{"preloadedState":{"doctorPage":{"doctor":{"reviewsForSeo":[{"text":"One"},{"text":"Two"}]}}}}}}'
    install_http(lambda request: httpx.Response(200, text=f'<script id="__NEXT_DATA__">{payload}</script>'))

    result = await main.fetch("https://docdoc.ru/doctor/a", Platform.SBERZDOROVIE, all_reviews=True)

    assert len(result["reviews"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, url", [
    ("sberzdorovie", "https://docdoc.ru/doctor/a"),
    ("prodoctorov", "https://prodoctorov.ru/doctor/a"),
])
async def test_api_captcha_with_http_200_returns_controlled_error(install_http, platform, url):
    install_http(lambda request: httpx.Response(200, text='<h1>Verify you are human</h1><script id="__NEXT_DATA__">{}</script>'))
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/getReviews", params={"url": url, "platform": platform, "all_reviews": "true"})

    assert response.status_code == 503
    assert response.json()["error"] == "source_blocked"


@pytest.mark.asyncio
async def test_api_returns_docdoc_reviews(install_http):
    payload = '{"props":{"pageProps":{"preloadedState":{"doctorPage":{"doctor":{"id":1}},"doctorReviews":{"reviewsForSeo":[{"id":1,"text":"Good"}],"totalReviewCount":1}}}}}'
    handler = install_http(lambda request: httpx.Response(200, text=f'<title>Doctor</title><script id="__NEXT_DATA__">{payload}</script>'))
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/getReviews", params={"url": "https://docdoc.ru/doctor/a", "platform": "sberzdorovie", "all_reviews": "true"})

    assert response.status_code == 200
    assert response.json()["title"] == "Doctor"
    assert response.json()["reviews"][0]["message"] == "Good"
    handler.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, url", [
    (Platform.SBERZDOROVIE, "https://docdoc.ru/doctor/a"),
    (Platform.PRODOCTOROV, "https://prodoctorov.ru/doctor/a"),
])
async def test_api_uses_and_caches_browser_fallback(install_http, browser_fallback, platform, url):
    browser_fallback.side_effect = None
    browser_fallback.return_value = CollectorResult(title="Врач", reviews=[Review(message="Хороший врач", source=platform.value)])
    handler = install_http(lambda request: httpx.Response(403, text="Forbidden"))
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/v1/getReviews", params={"url": url, "platform": platform.value})
        second = await client.get("/api/v1/getReviews", params={"url": url, "platform": platform.value})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["reviews"][0]["message"] == "Хороший врач"
    browser_fallback.assert_awaited_once_with(url, platform, False)
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_closes_browser_on_error(monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr(main, "close_browser", close)
    with pytest.raises(RuntimeError, match="shutdown"):
        async with main.lifespan(main.app):
            raise RuntimeError("shutdown")
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_rate_limit_returns_retry_after():
    main.app.state.rate_limiter = FixedWindowRateLimiter(1, 60)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/metrics")).status_code == 200
        response = await client.get("/metrics")
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_api_auth_missing_configured_key():
    main.API_AUTH_ENABLED = True
    main.API_KEY = ""
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")
    assert response.status_code == 500
    assert response.json() == {"error": "server_configuration_error"}
