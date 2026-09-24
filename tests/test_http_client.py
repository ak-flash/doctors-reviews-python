import gzip
import threading
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from curl_cffi.requests import exceptions as curl_errors

from collectors.base import InvalidURLError, SourceBlockedError, SourceHTTPError, is_blocked_content
from collectors.http_client import HTTPClient, ResponseTooLargeError


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["docdoc.ru", "ekb.docdoc.ru", "sberhealth.ru", "ekb.sberhealth.ru", "DOCdoc.ru.", "prodoctorov.ru"])
async def test_curl_browser_profile_and_response_adapter(curl_session_factory, host):
    loop_thread = threading.get_ident()
    validator = AsyncMock()
    url = f"https://{host}/doctor/a"
    content = "Отзывы врача".encode("utf-8")

    def handler(request):
        assert threading.get_ident() != loop_thread
        validator.assert_awaited_once_with(url)
        assert "text/html" in request.headers["Accept"]
        assert request.headers["Accept-Language"].startswith("ru")
        assert request.headers["Referer"].endswith("/")
        assert "User-Agent" not in request.headers
        return httpx.Response(200, content=gzip.compress(content), headers={"Content-Encoding": "gzip", "Content-Type": "text/html; charset=utf-8", "X-Source": "DocDoc"})

    session = curl_session_factory(handler)
    httpx_handler = Mock(side_effect=AssertionError("Configured sources must use curl_cffi"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(httpx_handler)) as transport:
        async with HTTPClient(("docdoc.ru", "sberhealth.ru", "prodoctorov.ru"), client=transport, curl_session=session, timeout=7, url_validator=validator) as client:
            response = await client.get(url)

    assert isinstance(response, httpx.Response)
    assert response.status_code == 200
    assert response.content == content
    assert response.text == "Отзывы врача"
    assert response.headers["x-source"] == "DocDoc"
    assert response.headers["content-encoding"] == "gzip"
    assert not response.is_redirect
    assert session.get.call_args.kwargs["impersonate"] == "chrome"
    assert session.get.call_args.kwargs["timeout"] == 7
    assert session.get.call_args.kwargs["allow_redirects"] is False
    session.close.assert_not_called()
    httpx_handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_proxy_is_passed_to_curl(curl_session_factory):
    session = curl_session_factory(lambda request: httpx.Response(200, text="Reviews"))
    async with HTTPClient(("docdoc.ru",), curl_session=session, url_validator=AsyncMock(), proxy="http://proxy.example:8080") as client:
        await client.get("https://docdoc.ru/doctor/a")

    assert session.get.call_args.kwargs["proxy"] == "http://proxy.example:8080"


@pytest.mark.asyncio
async def test_httpx_proxy_is_configured(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    client = HTTPClient(("example.org",), url_validator=AsyncMock())
    async with client:
        assert client._https_proxy == "http://proxy.example:8080"
        assert client._proxy_for("https://example.org") == "http://proxy.example:8080"


@pytest.mark.asyncio
async def test_other_domains_use_httpx(curl_session_factory):
    session = curl_session_factory(lambda request: pytest.fail("Unexpected curl request"))
    handler = Mock(return_value=httpx.Response(200, text="Reviews"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with HTTPClient(("example.org",), client=transport, curl_session=session, url_validator=AsyncMock()) as client:
            response = await client.get("https://example.org/doctor/a")

    assert response.content == b"Reviews"
    handler.assert_called_once()
    session.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, content, expected_status", [
    (200, "<h1>CAPTCHA</h1>", 503),
    (200, "<title>Access denied</title>", 503),
    (200, '<html><body><script src="https://servicepipe.tech/static/checkjs/example.js"></script></body></html>', 503),
    (403, "Forbidden", 403),
    (429, "Rate limited", 429),
    (503, "Unavailable", 503),
])
async def test_curl_blocked_responses_are_not_retried(curl_session_factory, status, content, expected_status):
    session = curl_session_factory(lambda request: httpx.Response(status, text=content))
    async with HTTPClient(("docdoc.ru",), curl_session=session, url_validator=AsyncMock()) as client:
        with pytest.raises(SourceBlockedError) as error:
            await client.get("https://docdoc.ru/doctor/a")

    assert error.value.status_code == expected_status
    session.get.assert_called_once()


@pytest.mark.parametrize("content", [
    '<title>Doctor</title><script>const captcha = "access denied";</script>',
    '<script id="__NEXT_DATA__">{"captchaEnabled":true}</script>',
    '<title>Doctor</title><style>.captcha {display:none}</style><!-- captcha -->',
])
def test_block_detection_ignores_scripts_styles_and_comments(content):
    assert not is_blocked_content(200, content)


def test_servicepipe_script_on_a_normal_page_is_not_a_block():
    content = '<h1>Doctor</h1><p>Reviews</p><script src="https://servicepipe.tech/static/checkjs/example.js"></script>'
    assert not is_blocked_content(200, content)


@pytest.mark.asyncio
async def test_curl_http_error_is_not_retried(curl_session_factory):
    session = curl_session_factory(lambda request: httpx.Response(404, text="Not found"))
    async with HTTPClient(("docdoc.ru",), curl_session=session, url_validator=AsyncMock()) as client:
        with pytest.raises(SourceHTTPError) as error:
            await client.get("https://docdoc.ru/doctor/a")

    assert error.value.status_code == 404
    session.get.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://docdoc.ru.attacker.invalid/doctor/a",
    "https://attacker-docdoc.ru/doctor/a",
    "http://127.0.0.1/doctor/a",
    "file://docdoc.ru/etc/passwd",
    "https://docdoc.ru:8443/doctor/a",
    "https://docdoc.ru:invalid/doctor/a",
    "https://user:pass@docdoc.ru/doctor/a",
])
async def test_disallowed_urls_never_reach_transport(curl_session_factory, url):
    validator = AsyncMock()
    session = curl_session_factory(lambda request: pytest.fail("Unsafe request"))
    async with HTTPClient(("docdoc.ru",), curl_session=session, url_validator=validator) as client:
        with pytest.raises(SourceHTTPError) as error:
            await client.get(url)

    assert error.value.status_code == 400
    validator.assert_not_called()
    session.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("location", [
    "https://attacker.invalid/",
    "https://docdoc.ru.attacker.invalid/",
    "http://127.0.0.1/",
    "file://docdoc.ru/etc/passwd",
    "https://user:pass@docdoc.ru/doctor/a",
    "https://docdoc.ru:8443/doctor/a",
    "https://docdoc.ru:invalid/doctor/a",
    "http://docdoc.ru/doctor/a",
    "https://[invalid/",
    "",
])
async def test_unsafe_redirects_never_reach_transport(curl_session_factory, location):
    validator = AsyncMock()
    session = curl_session_factory(lambda request: httpx.Response(302, headers={"Location": location}))
    async with HTTPClient(("docdoc.ru",), curl_session=session, url_validator=validator) as client:
        with pytest.raises(SourceHTTPError) as error:
            await client.get("https://docdoc.ru/doctor/a")

    assert error.value.status_code == 502
    validator.assert_awaited_once_with("https://docdoc.ru/doctor/a")
    session.get.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [False, True])
async def test_default_dns_validation_rejects_private_addresses(monkeypatch, curl_session_factory, redirect):
    resolved = []

    def resolver(host, port, *args):
        resolved.append(host)
        ip = "93.184.216.34" if redirect and host == "docdoc.ru" else "127.0.0.1"
        return [(0, 0, 0, "", (ip, port))]

    monkeypatch.setattr("app.security.socket.getaddrinfo", resolver)
    session = curl_session_factory(lambda request: httpx.Response(302, headers={"Location": "https://private.docdoc.ru/doctor/a"}))
    async with HTTPClient(("docdoc.ru",), curl_session=session) as client:
        with pytest.raises(InvalidURLError):
            await client.get("https://docdoc.ru/doctor/a")

    assert resolved == (["docdoc.ru", "private.docdoc.ru"] if redirect else ["docdoc.ru"])
    assert session.get.call_count == (1 if redirect else 0)


@pytest.mark.asyncio
async def test_allowed_redirects_validate_every_hop(curl_session_factory):
    validated = []

    async def validator(url):
        validated.append(url)

    def handler(request):
        assert str(request.url) == validated[-1]
        if request.url.host == "docdoc.ru":
            return httpx.Response(302, headers={"Location": "https://sberhealth.ru/doctor/a"})
        if request.url.path == "/doctor/a":
            return httpx.Response(301, headers={"Location": "/doctor/b"})
        return httpx.Response(200, text="Reviews")

    session = curl_session_factory(handler)
    async with HTTPClient(("docdoc.ru", "sberhealth.ru"), curl_session=session, url_validator=validator) as client:
        response = await client.get("https://docdoc.ru/doctor/a")

    assert response.content == b"Reviews"
    assert str(response.url) == "https://sberhealth.ru/doctor/b"
    assert validated == ["https://docdoc.ru/doctor/a", "https://sberhealth.ru/doctor/a", "https://sberhealth.ru/doctor/b"]
    assert all(call.kwargs["allow_redirects"] is False for call in session.get.call_args_list)


@pytest.mark.asyncio
async def test_redirect_limit(curl_session_factory):
    session = curl_session_factory(lambda request: httpx.Response(302, headers={"Location": "/doctor/a"}))
    async with HTTPClient(("docdoc.ru",), curl_session=session, url_validator=AsyncMock()) as client:
        with pytest.raises(SourceHTTPError, match="Too many redirects"):
            await client.get("https://docdoc.ru/doctor/a")

    assert session.get.call_count == 11


@pytest.mark.asyncio
async def test_injected_httpx_client_cannot_follow_unsafe_redirects():
    handler = Mock(return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1/"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as transport:
        async with HTTPClient(("example.org",), client=transport, url_validator=AsyncMock()) as client:
            with pytest.raises(SourceHTTPError):
                await client.get("https://example.org/doctor/a")

    handler.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["docdoc.ru", "example.org"])
async def test_response_limit_stops_download_before_full_body(curl_session_factory, host):
    received = []

    class Stream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            for chunk in [b"12345", b"67890", b"must not be read"]:
                received.append(chunk)
                yield chunk

        async def __aiter__(self):
            for chunk in self:
                yield chunk

    response = httpx.Response(200, stream=Stream())
    session = curl_session_factory(lambda request: response)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as transport:
        async with HTTPClient((host,), client=transport, curl_session=session, max_response_bytes=8, url_validator=AsyncMock()) as client:
            with pytest.raises(ResponseTooLargeError):
                await client.get(f"https://{host}/doctor/a")

    assert received == [b"12345", b"67890"]
    assert response.is_closed
    assert session.get.call_count == (1 if host == "docdoc.ru" else 0)


@pytest.mark.asyncio
async def test_response_at_size_limit_is_accepted(curl_session_factory):
    session = curl_session_factory(lambda request: httpx.Response(200, content=b"12345"))
    async with HTTPClient(("docdoc.ru",), curl_session=session, max_response_bytes=5, url_validator=AsyncMock()) as client:
        response = await client.get("https://docdoc.ru/doctor/a")

    assert response.content == b"12345"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [curl_errors.Timeout, curl_errors.ConnectionError])
@pytest.mark.parametrize("recover", [False, True])
async def test_curl_network_retries_and_timeout_mapping(monkeypatch, curl_session_factory, error_type, recover):
    sleep = AsyncMock()
    monkeypatch.setattr("collectors.http_client.asyncio.sleep", sleep)
    validator = AsyncMock()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if not recover or calls < 3:
            raise error_type("temporary network failure")
        return httpx.Response(200, text="Reviews")

    session = curl_session_factory(handler)
    async with HTTPClient(("docdoc.ru",), curl_session=session, retries=2, url_validator=validator) as client:
        if recover:
            assert (await client.get("https://docdoc.ru/doctor/a")).content == b"Reviews"
        else:
            with pytest.raises(SourceHTTPError) as error:
                await client.get("https://docdoc.ru/doctor/a")
            assert error.value.status_code == 504

    assert calls == validator.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.2, 0.4]


@pytest.mark.asyncio
async def test_curl_configuration_error_is_controlled_without_retries(curl_session_factory):
    def handler(request):
        raise curl_errors.ImpersonateError("Invalid profile")

    session = curl_session_factory(handler)
    async with HTTPClient(("docdoc.ru",), curl_session=session, url_validator=AsyncMock()) as client:
        with pytest.raises(SourceHTTPError) as error:
            await client.get("https://docdoc.ru/doctor/a")

    assert error.value.status_code == 502
    session.get.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_owned_curl_session_closes_after_success_or_error(monkeypatch, curl_session_factory, blocked):
    session = curl_session_factory(lambda request: httpx.Response(403 if blocked else 200, text="Response"))
    factory = Mock(return_value=session)
    monkeypatch.setattr("collectors.http_client.curl_requests.Session", factory)
    async with HTTPClient(("docdoc.ru",), url_validator=AsyncMock()) as client:
        if blocked:
            with pytest.raises(SourceBlockedError):
                await client.get("https://docdoc.ru/doctor/a")
        else:
            await client.get("https://docdoc.ru/doctor/a")

    factory.assert_called_once_with(use_thread_local_curl=False)
    session.close.assert_called_once()
