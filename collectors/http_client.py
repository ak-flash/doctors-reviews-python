from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlparse

import httpx

from app.security import validate_public_dns
from .base import SourceHTTPError, SourceBlockedError, is_blocked_content

URLValidator = Callable[[str], Awaitable[None]]
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

logger = logging.getLogger(__name__)


class ResponseTooLargeError(SourceHTTPError):
    status_code = 413
    code = "response_too_large"


def _buffered_response(response: httpx.Response, content: bytes, url: str) -> httpx.Response:
    result = httpx.Response(response.status_code, content=content, request=httpx.Request("GET", url))
    result.headers = httpx.Headers(response.headers.multi_items())
    return result


class HTTPClient:
    def __init__(
        self,
        allowed_domains: Iterable[str],
        *,
        timeout: float = 20,
        max_response_bytes: int = 5_000_000,
        retries: int = 2,
        max_redirects: int = 10,
        client: httpx.AsyncClient | None = None,
        url_validator: URLValidator = validate_public_dns,
        proxy: str | None = None,
    ):
        self.allowed_domains = tuple(domain.lower().rstrip(".") for domain in allowed_domains)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.retries = retries
        self.max_redirects = max_redirects
        self.url_validator = url_validator
        self.proxy = proxy if proxy is not None else os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "HTTPClient":
        if self._client is None:
            # Without an explicit proxy, httpx applies HTTP_PROXY/HTTPS_PROXY/NO_PROXY from the environment per URL scheme.
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, proxy=self.proxy)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _allowed(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
                return False
            if parsed.port not in (None, 80, 443):
                return False
            host = parsed.hostname
        except ValueError:
            return False
        return bool(host and any(host.lower().rstrip(".") == domain or host.lower().rstrip(".").endswith("." + domain) for domain in self.allowed_domains))

    def _append_content(self, content: bytearray, chunk: bytes) -> int:
        if len(content) + len(chunk) > self.max_response_bytes:
            raise ResponseTooLargeError("Source response exceeds configured limit")
        content.extend(chunk)
        return len(chunk)

    async def _get(self, url: str, headers: Mapping[str, str] | None) -> httpx.Response:
        assert self._client is not None
        parsed = urlparse(url)
        request_headers = httpx.Headers({**BROWSER_HEADERS, "Referer": f"{parsed.scheme}://{parsed.netloc}/"})
        request_headers.update(headers or {})
        async with self._client.stream("GET", url, headers=request_headers, timeout=self.timeout, follow_redirects=False) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                self._append_content(content, chunk)
            return _buffered_response(response, bytes(content), url)

    async def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
        if not self._allowed(url):
            raise SourceHTTPError("Redirect or URL points to a disallowed domain", status_code=400)
        assert self._client is not None, "HTTPClient must be used as async context manager"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            target = url
            redirects = 0
            try:
                while True:
                    await self.url_validator(target)
                    started = time.perf_counter()
                    response = await self._get(target, headers)
                    elapsed = (time.perf_counter() - started) * 1000
                    logger.info("HTTP GET status=%s elapsed_ms=%.1f url=%s", response.status_code, elapsed, target)
                    if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        try:
                            redirected = urljoin(target, location) if location else ""
                        except ValueError as exc:
                            raise SourceHTTPError("Invalid redirect URL", status_code=502) from exc
                        if not redirected or not self._allowed(redirected):
                            raise SourceHTTPError("Redirect URL is not allowed", status_code=502)
                        if urlparse(target).scheme == "https" and urlparse(redirected).scheme != "https":
                            raise SourceHTTPError("HTTPS downgrade redirect is not allowed", status_code=502)
                        redirects += 1
                        if redirects > self.max_redirects:
                            raise SourceHTTPError("Too many redirects", status_code=502)
                        target = redirected
                        continue
                    if is_blocked_content(response.status_code, response.content):
                        status_code = response.status_code if response.status_code >= 400 else 503
                        raise SourceBlockedError("Source returned CAPTCHA or blocking page", status_code=status_code)
                    if response.status_code >= 400:
                        raise SourceHTTPError(f"Source returned HTTP {response.status_code}", status_code=response.status_code)
                    return response
            except (SourceBlockedError, ResponseTooLargeError):
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as exc:
                # httpx parses Location even when redirects are not followed; a malformed one is a bad upstream response, not a transient failure.
                if isinstance(exc.__context__, httpx.InvalidURL):
                    raise SourceHTTPError("Invalid redirect URL", status_code=502) from exc
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.2 * (2**attempt))
            except httpx.RequestError as exc:
                raise SourceHTTPError(f"HTTP request failed: {exc}", status_code=502) from exc
        raise SourceHTTPError(f"HTTP request failed: {last_error}", status_code=504) from last_error
