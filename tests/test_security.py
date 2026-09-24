import httpx
import pytest

from app.cache import AsyncTTLCache
from app.limiter import FixedWindowRateLimiter
from app.security import validate_public_dns, validate_url_shape
from collectors.base import InvalidURLError, Platform


@pytest.mark.asyncio
async def test_cache_expiration_key_isolation_and_eviction():
    now = [0.0]
    cache = AsyncTTLCache(5, 2, clock=lambda: now[0])
    await cache.set("sberzdorovie:https://docdoc.ru/doctor/a:page", {"mode": "page"})
    await cache.set("sberzdorovie:https://docdoc.ru/doctor/a:all", {"mode": "all"})
    assert await cache.get("sberzdorovie:https://docdoc.ru/doctor/a:page") == {"mode": "page"}
    assert await cache.get("sberzdorovie:https://docdoc.ru/doctor/a:all") == {"mode": "all"}
    await cache.set("prodoctorov:https://prodoctorov.ru/doctor/a:page", {"mode": "other"})
    assert await cache.get("sberzdorovie:https://docdoc.ru/doctor/a:page") is None
    now[0] = 6
    assert await cache.get("sberzdorovie:https://docdoc.ru/doctor/a:all") is None


@pytest.mark.asyncio
async def test_rate_limiter_returns_retry_after():
    limiter = FixedWindowRateLimiter(1, 60)
    assert (await limiter.check("ip:key"))[0]
    allowed, retry = await limiter.check("ip:key")
    assert not allowed
    assert retry > 0


def test_url_rejects_userinfo_and_ports():
    with pytest.raises(InvalidURLError):
        validate_url_shape("https://user:pass@docdoc.ru/doctor/a", Platform.SBERZDOROVIE)
    with pytest.raises(InvalidURLError):
        validate_url_shape("https://docdoc.ru:8443/doctor/a", Platform.SBERZDOROVIE)


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "192.168.1.2", "169.254.1.1", "224.0.0.1", "0.0.0.0"])
async def test_dns_rejects_non_public_addresses(ip):
    async def resolver(host, port):
        return [(0, 0, 0, "", (ip, port))]

    with pytest.raises(InvalidURLError):
        await validate_public_dns("https://docdoc.ru/doctor/a", resolver)


@pytest.mark.asyncio
async def test_dns_accepts_public_ipv4_and_ipv6():
    async def resolver(host, port):
        return [(0, 0, 0, "", ("93.184.216.34", port)), (0, 0, 0, "", ("2606:2800:220:1:248:1893:25c8:1946", port, 0, 0))]

    await validate_public_dns("https://docdoc.ru/doctor/a", resolver)


@pytest.mark.asyncio
async def test_api_key_auth(monkeypatch):
    import main

    main.API_AUTH_ENABLED = True
    main.API_KEY = "test-key"
    main.app.state.rate_limiter = FixedWindowRateLimiter(100, 60)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/getReviews")).status_code == 401
        assert (await client.get("/api/v1/getReviews", headers={"X-API-Key": "test-key"})).status_code == 400
    main.API_AUTH_ENABLED = False
