import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from collectors.base import InvalidURLError, Platform, SourceBlockedError, SourceHTTPError
from collectors.browser import BrowserUnavailableError, CamoufoxClient
from collectors.http_client import ResponseTooLargeError


SBER_URL = "https://docdoc.ru/doctor/a"
PRO_URL = "https://prodoctorov.ru/doctor/a"
PRO_HTML = '<title>Врач</title><div class="b-review-card"><div itemprop="reviewBody" data="1"></div><div class="b-review-card__comment">Хороший врач</div></div>'


def sber_html(total=1):
    data = {"props": {"pageProps": {"preloadedState": {
        "doctorPage": {"doctor": {"id": 42}},
        "doctorReviews": {"reviewsForSeo": [{"id": 1, "text": "Хороший врач"}], "totalReviewCount": total},
    }}}}
    return '<title>Врач</title><script id="__NEXT_DATA__">' + json.dumps(data, ensure_ascii=False) + '</script>'


class FakePage:
    def __init__(self, content, status=200):
        self.url = "about:blank"
        self.main_frame = object()
        self.status = status
        self.content = AsyncMock(return_value=content)
        self.close = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.evaluate = AsyncMock()
        self.guard = None

    async def route(self, pattern, guard):
        self.guard = guard

    async def request(self, url, *, main=True, resource_type="document"):
        request = SimpleNamespace(
            url=url,
            frame=self.main_frame if main else object(),
            resource_type=resource_type,
            is_navigation_request=lambda: main,
        )
        route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock())
        await self.guard(route, request)
        return route

    async def goto(self, url, **kwargs):
        self.url = url
        route = await self.request(url)
        if route.abort.called:
            raise RuntimeError("Navigation aborted")
        return SimpleNamespace(status=self.status)


@pytest.fixture
def make_browser(tmp_path):
    def make(*pages, **options):
        context = SimpleNamespace(pages=[], on=Mock(), new_page=AsyncMock(side_effect=pages))
        manager = SimpleNamespace(__aenter__=AsyncMock(return_value=context), __aexit__=AsyncMock())
        factory = AsyncMock(return_value=manager)
        validator = options.pop("url_validator", AsyncMock())
        client = CamoufoxClient(profile_dir=str(tmp_path / "profile"), factory=factory, url_validator=validator, **options)
        return client, context, manager, factory
    return make


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_browser_proxy_is_passed_to_camoufox(make_browser):
    page = FakePage(sber_html())
    client, _, _, factory = make_browser(page, proxy="http://proxy.example:8080")
    await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    assert factory.call_args.kwargs["proxy"] == {"server": "http://proxy.example:8080"}
    await client.close()


@pytest.mark.asyncio
async def test_one_lazy_context_for_both_sources(make_browser):
    sber = FakePage(sber_html())
    pro = FakePage(PRO_HTML)
    client, context, manager, factory = make_browser(sber, pro)
    factory.assert_not_called()
    try:
        first = await client.collect(SBER_URL, Platform.SBERZDOROVIE)
        second = await client.collect(PRO_URL, Platform.PRODOCTOROV)
        assert first.reviews[0].message == second.reviews[0].message == "Хороший врач"
        assert first.reviews[0].source == "sberzdorovie"
        assert second.reviews[0].source == "prodoctorov"
        factory.assert_awaited_once()
        assert factory.call_args.kwargs["persistent_context"] is True
        assert factory.call_args.kwargs["block_webgl"] is True
        assert context.new_page.await_count == 2
        assert sber.wait_for_selector.call_args.kwargs["state"] == "attached"
        sber.close.assert_awaited_once()
        pro.close.assert_awaited_once()
    finally:
        await client.close()
    manager.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_launch_does_not_close_restored_pages(make_browser):
    startup_page = FakePage(sber_html())
    result_page = FakePage(sber_html())
    client, context, _, _ = make_browser(result_page)
    context.pages.append(startup_page)
    try:
        await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    finally:
        await client.close()
    startup_page.close.assert_not_awaited()
    result_page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_parallel_requests_use_at_most_one_page(make_browser):
    entered, release = asyncio.Event(), asyncio.Event()
    first, second = FakePage(sber_html()), FakePage(PRO_HTML)

    async def wait(*args, **kwargs):
        entered.set()
        await release.wait()

    first.wait_for_selector.side_effect = wait
    client, context, _, factory = make_browser(first, second)
    one = asyncio.create_task(client.collect(SBER_URL, Platform.SBERZDOROVIE))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        two = asyncio.create_task(client.collect(PRO_URL, Platform.PRODOCTOROV))
        await asyncio.sleep(0)
        assert context.new_page.await_count == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(one, two), 2)
        assert context.new_page.await_count == 2
        factory.assert_awaited_once()
    finally:
        release.set()
        await client.close()


@pytest.mark.asyncio
async def test_cancelled_request_closes_tab_and_keeps_context_usable(make_browser):
    entered = asyncio.Event()
    page, next_page = FakePage(sber_html()), FakePage(PRO_HTML)

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    page.wait_for_selector.side_effect = wait
    client, _, manager, factory = make_browser(page, next_page)
    task = asyncio.create_task(client.collect(SBER_URL, Platform.SBERZDOROVIE))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    page.close.assert_awaited_once()
    await client.collect(PRO_URL, Platform.PRODOCTOROV)
    factory.assert_awaited_once()
    await client.close()
    manager.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_launch_failure_cleans_up_and_can_retry(make_browser):
    client, context, manager, factory = make_browser(FakePage(sber_html()))
    manager.__aenter__.side_effect = RuntimeError("browser executable missing")
    with pytest.raises(BrowserUnavailableError):
        await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    manager.__aexit__.assert_awaited_once()
    manager.__aenter__.side_effect = None
    result = await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    assert result.reviews
    assert factory.await_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_closed_context_is_replaced(make_browser):
    client, context, manager, factory = make_browser(FakePage(sber_html()), FakePage(sber_html()))
    await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    callback = context.on.call_args.args[1]
    callback()
    await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    assert factory.await_count == 2
    manager.__aexit__.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_browser_http_block_closes_page(make_browser, status):
    page = FakePage("Forbidden", status=status)
    client, _, _, _ = make_browser(page)
    with pytest.raises(SourceBlockedError) as error:
        await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    assert error.value.status_code == status
    page.close.assert_awaited_once()
    page.wait_for_selector.assert_not_called()
    await client.close()


@pytest.mark.asyncio
async def test_browser_http_404_is_not_a_captcha(make_browser):
    page = FakePage("Not found", status=404)
    client, _, _, _ = make_browser(page)
    with pytest.raises(SourceHTTPError) as error:
        await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    assert error.value.status_code == 404
    page.close.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    '<script src="https://servicepipe.tech/static/checkjs/x.js"></script>',
    '<p>Мы хотим убедиться, что имеем дело именно с вами, а не с ботом.</p><script src="./sp_rotated_captcha/js/bundle.js"></script>',
])
async def test_selector_timeout_returns_controlled_source_error(make_browser, content):
    page = FakePage(content)
    page.wait_for_selector.side_effect = TimeoutError("verification not complete")
    client, _, _, _ = make_browser(page)
    with pytest.raises(SourceBlockedError) as error:
        await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    assert error.value.status_code == 503
    page.close.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
async def test_selector_timeout_still_parses_loaded_page(make_browser):
    page = FakePage(sber_html())
    page.wait_for_selector.side_effect = TimeoutError("selector changed")
    client, _, _, _ = make_browser(page)
    result = await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    assert result.reviews[0].message == "Хороший врач"
    page.close.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
async def test_browser_dom_size_limit(make_browser):
    page = FakePage(sber_html())
    client, _, _, _ = make_browser(page, max_response_bytes=8)
    with pytest.raises(ResponseTooLargeError):
        await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    page.close.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1/", "https://attacker.invalid/", "https://user:pass@docdoc.ru/", "https://docdoc.ru:8080/"])
async def test_invalid_url_does_not_launch_browser(make_browser, url):
    client, _, _, factory = make_browser()
    with pytest.raises(InvalidURLError):
        await client.collect(url, Platform.SBERZDOROVIE)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_private_dns_does_not_launch_browser(make_browser):
    validator = AsyncMock(side_effect=InvalidURLError("Private IP"))
    client, _, _, factory = make_browser(url_validator=validator)
    with pytest.raises(InvalidURLError):
        await client.collect(SBER_URL, Platform.SBERZDOROVIE)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_navigation_guard_and_heavy_resource_filter(make_browser):
    page = FakePage(sber_html())
    client, _, _, _ = make_browser(page)
    failures = []
    await client._install_routes(page, Platform.SBERZDOROVIE, SBER_URL, failures)
    external = await page.request("https://attacker.invalid/")
    external.abort.assert_awaited_once()
    assert isinstance(failures[0], InvalidURLError)
    video = await page.request("https://docdoc.ru/video.mp4", main=False, resource_type="media")
    video.abort.assert_awaited_once()
    allowed = await page.request("https://docdoc.ru/doctor/b")
    allowed.continue_.assert_awaited_once()
    client.url_validator.assert_awaited_once_with("https://docdoc.ru/doctor/b")


@pytest.mark.asyncio
async def test_more_reviews_use_same_browser_page(make_browser):
    page = FakePage(sber_html(total=2))
    page.evaluate.return_value = {"status": 200, "content": json.dumps({"reviews": [{"id": 2, "text": "Ещё отзыв"}]})}
    client, _, _, _ = make_browser(page)
    result = await client.collect(SBER_URL, Platform.SBERZDOROVIE, all_reviews=True)
    assert [review.message for review in result.reviews] == ["Хороший врач", "Ещё отзыв"]
    args = page.evaluate.call_args.args[1]
    assert "doctorId=42" in args["url"] and "offset=1" in args["url"]
    page.close.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
async def test_page_mode_does_not_fetch_additional_reviews(make_browser):
    page = FakePage(sber_html(total=2))
    client, _, _, _ = make_browser(page)
    result = await client.collect(SBER_URL, Platform.SBERZDOROVIE, all_reviews=False)
    assert len(result.reviews) == 1
    page.evaluate.assert_not_called()
    await client.close()
