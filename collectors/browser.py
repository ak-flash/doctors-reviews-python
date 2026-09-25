from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from app.security import validate_public_dns, validate_url_shape
from .base import CollectorError, CollectorResult, EmptyResponseError, InvalidURLError, Platform, SourceBlockedError, SourceHTTPError, is_blocked_content
from .http_client import ResponseTooLargeError, URLValidator

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)
BrowserFactory = Callable[..., Awaitable[Any]]
_browser: CamoufoxClient | None = None
stats = {"fallbacks": 0, "browser_launches": 0}


class BrowserUnavailableError(SourceHTTPError):
    status_code = 503
    code = "browser_unavailable"


async def _create_camoufox(**options):
    module = await asyncio.to_thread(importlib.import_module, "camoufox.async_api")
    return module.AsyncCamoufox(**options)


class CamoufoxClient:
    def __init__(
        self,
        *,
        profile_dir: str = "data/browser",
        headless: bool | str = False,
        timeout: float = 60,
        max_response_bytes: int = 5_000_000,
        url_validator: URLValidator = validate_public_dns,
        factory: BrowserFactory | None = None,
        proxy: str | None = None,
    ):
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.url_validator = url_validator
        self.proxy = proxy if proxy is not None else os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        self._factory = factory or _create_camoufox
        self._lock = asyncio.Lock()
        self._manager = None
        self._context: BrowserContext | None = None
        self._context_closed = False

    async def _close_unlocked(self) -> None:
        manager, self._manager = self._manager, None
        self._context = None
        self._context_closed = False
        if manager is not None:
            await manager.__aexit__(None, None, None)

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _get_context(self) -> BrowserContext:
        if self._context is not None and not self._context_closed:
            return self._context
        if self._manager is not None:
            await self._close_unlocked()
        try:
            await asyncio.to_thread(Path(self.profile_dir).mkdir, parents=True, exist_ok=True)
            self._manager = await self._factory(
                headless=self.headless,
                persistent_context=True,
                user_data_dir=self.profile_dir,
                humanize=True,
                locale="ru-RU",
                service_workers="block",
                proxy={"server": self.proxy} if self.proxy else None,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            self._context = await self._manager.__aenter__()
            self._context_closed = False
            self._context.on("close", self._on_context_closed)
            await asyncio.sleep(2)
            stats["browser_launches"] += 1
            logger.info("Camoufox started headless=%s", self.headless)
            return self._context
        except BaseException:
            with suppress(Exception):
                await self._close_unlocked()
            raise

    def _on_context_closed(self, *args) -> None:
        self._context_closed = True

    async def _install_routes(self, page: Page, platform: Platform, start_url: str, failures: list[CollectorError]) -> None:
        navigations = 0

        async def guard(route, request):
            nonlocal navigations
            main_navigation = request.is_navigation_request() and request.frame == page.main_frame
            try:
                parsed = urlparse(request.url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 80, 443):
                    raise InvalidURLError("Browser request URL is not allowed")
                if main_navigation:
                    validate_url_shape(request.url, platform)
                    if urlparse(start_url).scheme == "https" and parsed.scheme != "https":
                        raise InvalidURLError("Browser HTTPS downgrade is not allowed")
                    navigations += 1
                    if navigations > 11:
                        raise SourceHTTPError("Too many browser redirects")
                if request.resource_type in {"media", "font"}:
                    await route.abort()
                    return
                await self.url_validator(request.url)
            except (CollectorError, ValueError) as error:
                if main_navigation:
                    failures.append(error if isinstance(error, CollectorError) else InvalidURLError("Invalid browser URL"))
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", guard)

    async def collect(self, url: str, platform: Platform, all_reviews: bool = False) -> CollectorResult:
        url = validate_url_shape(url, platform)
        await self.url_validator(url)
        started = time.perf_counter()
        async with self._lock:
            page = None
            failures: list[CollectorError] = []
            context_ready = False
            try:
                async with asyncio.timeout(self.timeout):
                    context = await self._get_context()
                    context_ready = True
                    for attempt in range(3):
                        try:
                            page = await context.new_page()
                            break
                        except Exception:
                            if attempt == 2:
                                raise
                            await asyncio.sleep(1)
                    await self._install_routes(page, platform, url, failures)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    if failures:
                        raise failures[0]
                    status = response.status if response is not None else 200
                    if status in {403, 429, 503}:
                        raise SourceBlockedError("Browser returned HTTP blocking page", status_code=status)
                    if status >= 400:
                        raise SourceHTTPError(f"Browser returned HTTP {status}", status_code=status)
                    selector = '#__NEXT_DATA__' if platform == Platform.SBERZDOROVIE else '.b-review-card__comment'
                    try:
                        await page.wait_for_selector(selector, state="attached", timeout=min(self.timeout * 1000, 15000))
                    except Exception:
                        logger.warning("Browser selector not found platform=%s selector=%s; parsing current page", platform.value, selector)
                    if platform == Platform.SBERZDOROVIE:
                        # Next.js can attach __NEXT_DATA__ before it fills <title>.
                        with suppress(Exception):
                            await page.wait_for_function("document.title.length > 0", timeout=3000)
                    validate_url_shape(page.url, platform)
                    content = await page.content()
                    if is_blocked_content(status, content):
                        raise SourceBlockedError("Browser returned a verification or blocking page", status_code=status)
                    if len(content.encode("utf-8")) > self.max_response_bytes:
                        raise ResponseTooLargeError("Browser response exceeds configured limit")
                    if failures:
                        raise failures[0]
                    if platform == Platform.SBERZDOROVIE:
                        from .sberzdorovie import _load_more_reviews, parse_sber_page

                        result, doctor_id, total = parse_sber_page(content)
                        if all_reviews and doctor_id and len(result.reviews) < total:
                            requester = _BrowserRequests(self, page, platform)
                            result.reviews.extend(await _load_more_reviews(requester, doctor_id, len(result.reviews), page.url))
                    else:
                        from .prodoctorov import parse_prodoctorov_page

                        result = parse_prodoctorov_page(content, all_reviews)
                    logger.info("Camoufox collection platform=%s reviews=%s elapsed_ms=%.1f", platform.value, len(result.reviews), (time.perf_counter() - started) * 1000)
                    return result
            except CollectorError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if failures:
                    raise failures[0] from error
                logger.exception("Camoufox collection failed platform=%s url=%s", platform.value, url)
                if not context_ready:
                    raise BrowserUnavailableError("Camoufox could not start; check browser installation and display") from error
                raise SourceBlockedError("Browser did not load review data; source verification may still be required") from error
            finally:
                if page is not None:
                    with suppress(Exception):
                        await page.close()


class _BrowserRequests:
    def __init__(self, client: CamoufoxClient, page: Page, platform: Platform):
        self.client = client
        self.page = page
        self.platform = platform

    async def get(self, url: str, *, headers=None) -> httpx.Response:
        url = validate_url_shape(url, self.platform)
        await self.client.url_validator(url)
        headers = {key: value for key, value in (headers or {}).items() if key.lower() not in {"origin", "referer"} and not key.lower().startswith("sec-")}
        payload = await self.page.evaluate("""async ({url, headers, limit}) => {
            const response = await fetch(url, {headers, credentials: 'include', redirect: 'error'});
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let size = 0, content = '';
            while (true) {
                const {value, done} = await reader.read();
                if (done) break;
                size += value.byteLength;
                if (size > limit) { await reader.cancel(); return {too_large: true}; }
                content += decoder.decode(value, {stream: true});
            }
            return {status: response.status, content: content + decoder.decode()};
        }""", {"url": url, "headers": headers, "limit": self.client.max_response_bytes})
        if payload.get("too_large"):
            raise ResponseTooLargeError("Browser response exceeds configured limit")
        if is_blocked_content(payload["status"], payload["content"]):
            raise SourceBlockedError("Source blocked additional reviews in browser")
        if payload["status"] >= 400:
            raise SourceHTTPError(f"Source returned HTTP {payload['status']}", status_code=payload["status"])
        return httpx.Response(payload["status"], text=payload["content"], request=httpx.Request("GET", url))


async def collect_with_browser(url: str, platform: Platform, all_reviews: bool = False) -> CollectorResult:
    global _browser
    if _browser is None:
        mode = os.getenv("BROWSER_HEADLESS", "false").lower()
        if mode not in {"true", "false", "virtual"}:
            raise BrowserUnavailableError("BROWSER_HEADLESS must be true, false or virtual")
        _browser = CamoufoxClient(
            profile_dir=os.getenv("BROWSER_PROFILE_DIR", "data/browser"),
            headless="virtual" if mode == "virtual" else mode == "true",
            timeout=float(os.getenv("BROWSER_TIMEOUT_SECONDS", "60")),
        )
    stats["fallbacks"] += 1
    return await _browser.collect(url, platform, all_reviews)


async def close_browser() -> None:
    global _browser
    browser, _browser = _browser, None
    if browser is not None:
        await browser.close()
