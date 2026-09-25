import asyncio
import contextlib
import gzip
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from collectors.base import InvalidURLError, SourceBlockedError, SourceHTTPError, is_blocked_content
from collectors.http_client import HTTPClient, ResponseTooLargeError


@contextlib.asynccontextmanager
async def fake_proxy():
    request_lines = []

    async def handle(reader, writer):
        request_lines.append((await reader.readline()).decode().strip())
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", request_lines


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["docdoc.ru", "ekb.docdoc.ru", "sberhealth.ru", "ekb.sberhealth.ru", "DOCdoc.ru.", "prodoctorov.ru"])
async def test_browser_headers_and_response_adapter(mock_client, host):
    validator = AsyncMock()
    url = f"https://{host}/doctor/a"
    content = "Отзывы врача".encode("utf-8")

    def handler(request):
        validator.assert_awaited_once_with(url)
        assert "text/html" in request.headers["Accept"]
        assert request.headers["Accept-Language"].startswith("ru")
        assert request.headers["Referer"] == f"https://{host}/"
        assert request.headers["User-Agent"].startswith("Mozilla/5.0")
        assert request.extensions["timeout"]["read"] == 7
        return httpx.Response(200, content=gzip.compress(content), headers={"Content-Encoding": "gzip", "Content-Type": "text/html; charset=utf-8", "X-Source": "DocDoc"})

    async with HTTPClient(("docdoc.ru", "sberhealth.ru", "prodoctorov.ru"), client=mock_client(handler), timeout=7, url_validator=validator) as client:
        response = await client.get(url)

    assert isinstance(response, httpx.Response)
    assert response.status_code == 200
    assert response.content == content
    assert response.text == "Отзывы врача"
    assert response.headers["x-source"] == "DocDoc"
    assert response.headers["content-encoding"] == "gzip"
    assert not response.is_redirect


@pytest.mark.asyncio
async def test_request_headers_override_browser_defaults(mock_client):
    def handler(request):
        assert request.headers.get_list("Accept") == ["application/json"]
        assert request.headers.get_list("Referer") == ["https://ekb.docdoc.ru/doctor/a"]
        return httpx.Response(200, json={})

    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), url_validator=AsyncMock()) as client:
        await client.get("https://docdoc.ru/doctors/moreReviews", headers={"accept": "application/json", "referer": "https://ekb.docdoc.ru/doctor/a"})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["argument", "environment"])
async def test_requests_use_configured_proxy(monkeypatch, source):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    async with fake_proxy() as (proxy, request_lines):
        if source == "environment":
            monkeypatch.setenv("HTTPS_PROXY", proxy)
        async with HTTPClient(("docdoc.ru",), retries=0, url_validator=AsyncMock(), proxy=proxy if source == "argument" else None) as client:
            with pytest.raises(SourceHTTPError) as error:
                await client.get("https://docdoc.ru/doctor/a")

    assert request_lines == ["CONNECT docdoc.ru:443 HTTP/1.1"]
    assert error.value.status_code == 504


@pytest.mark.asyncio
@pytest.mark.parametrize("status, content, expected_status", [
    (200, "<h1>CAPTCHA</h1>", 503),
    (200, "<title>Access denied</title>", 503),
    (200, '<html><body><script src="https://servicepipe.tech/static/checkjs/example.js"></script></body></html>', 503),
    (200, '<div id="captcha_root"><p>Мы хотим убедиться, что имеем дело именно с вами, а не с ботом.</p></div><script src="./sp_rotated_captcha/js/bundle.js"></script>', 503),
    (403, "Forbidden", 403),
    (429, "Rate limited", 429),
    (503, "Unavailable", 503),
])
async def test_blocked_responses_are_not_retried(mock_client, status, content, expected_status):
    handler = Mock(side_effect=lambda request: httpx.Response(status, text=content))
    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), url_validator=AsyncMock()) as client:
        with pytest.raises(SourceBlockedError) as error:
            await client.get("https://docdoc.ru/doctor/a")

    assert error.value.status_code == expected_status
    handler.assert_called_once()


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
async def test_http_error_is_not_retried(mock_client):
    handler = Mock(side_effect=lambda request: httpx.Response(404, text="Not found"))
    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), url_validator=AsyncMock()) as client:
        with pytest.raises(SourceHTTPError) as error:
            await client.get("https://docdoc.ru/doctor/a")

    assert error.value.status_code == 404
    handler.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://docdoc.ru.attacker.invalid/doctor/a",
    "https://attacker-docdoc.ru/doctor/a",
    "http://127.0.0.1/doctor/a",
    "file://docdoc.ru/etc/passwd",
    "https://docdoc.ru:8443/doctor/a",
    "https://docdoc.ru:invalid/doctor/a",
    "https://user:********@docdoc.ru/doctor/a",
])
async def test_disallowed_urls_never_reach_transport(mock_client, url):
    validator = AsyncMock()
    handler = Mock(side_effect=AssertionError("Unsafe request"))
    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), url_validator=validator) as client:
        with pytest.raises(SourceHTTPError) as error:
            await client.get(url)

    assert error.value.status_code == 400
    validator.assert_not_called()
    handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("location", [
    "https://attacker.invalid/",
    "https://docdoc.ru.attacker.invalid/",
    "http://127.0.0.1/",
    "file://docdoc.ru/etc/passwd",
    "https://user:********@docdoc.ru/doctor/a",
    "https://docdoc.ru:8443/doctor/a",
    "https://docdoc.ru:invalid/doctor/a",
    "http://docdoc.ru/doctor/a",
    "https://[invalid/",
    "",
])
async def test_unsafe_redirects_never_reach_transport(mock_client, location):
    validator = AsyncMock()
    handler = Mock(side_effect=lambda request: httpx.Response(302, headers={"Location": location}))
    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), url_validator=validator) as client:
        with pytest.raises(SourceHTTPError) as error:
            await client.get("https://docdoc.ru/doctor/a")

    assert error.value.status_code == 502
    validator.assert_awaited_once_with("https://docdoc.ru/doctor/a")
    handler.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [False, True])
async def test_default_dns_validation_rejects_private_addresses(monkeypatch, mock_client, redirect):
    resolved = []

    def resolver(host, port, *args):
        resolved.append(host)
        ip = "93.184.216.34" if redirect and host == "docdoc.ru" else "127.0.0.1"
        return [(0, 0, 0, "", (ip, port))]

    monkeypatch.setattr("app.security.socket.getaddrinfo", resolver)
    handler = Mock(side_effect=lambda request: httpx.Response(302, headers={"Location": "https://private.docdoc.ru/doctor/a"}))
    async with HTTPClient(("docdoc.ru",), client=mock_client(handler)) as client:
        with pytest.raises(InvalidURLError):
            await client.get("https://docdoc.ru/doctor/a")

    assert resolved == (["docdoc.ru", "private.docdoc.ru"] if redirect else ["docdoc.ru"])
    assert handler.call_count == (1 if redirect else 0)


@pytest.mark.asyncio
async def test_allowed_redirects_validate_every_hop(mock_client):
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

    async with HTTPClient(("docdoc.ru", "sberhealth.ru"), client=mock_client(handler), url_validator=validator) as client:
        response = await client.get("https://docdoc.ru/doctor/a")

    assert response.content == b"Reviews"
    assert str(response.url) == "https://sberhealth.ru/doctor/b"
    assert validated == ["https://docdoc.ru/doctor/a", "https://sberhealth.ru/doctor/a", "https://sberhealth.ru/doctor/b"]


@pytest.mark.asyncio
async def test_redirect_limit(mock_client):
    handler = Mock(side_effect=lambda request: httpx.Response(302, headers={"Location": "/doctor/a"}))
    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), url_validator=AsyncMock()) as client:
        with pytest.raises(SourceHTTPError, match="Too many redirects"):
            await client.get("https://docdoc.ru/doctor/a")

    assert handler.call_count == 11


@pytest.mark.asyncio
async def test_injected_httpx_client_cannot_follow_unsafe_redirects():
    handler = Mock(return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1/"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as transport:
        async with HTTPClient(("example.org",), client=transport, url_validator=AsyncMock()) as client:
            with pytest.raises(SourceHTTPError):
                await client.get("https://example.org/doctor/a")

    handler.assert_called_once()


@pytest.mark.asyncio
async def test_response_limit_stops_download_before_full_body(mock_client):
    received = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in [b"12345", b"67890", b"must not be read"]:
                received.append(chunk)
                yield chunk

    response = httpx.Response(200, stream=Stream())
    async with HTTPClient(("docdoc.ru",), client=mock_client(lambda request: response), max_response_bytes=8, url_validator=AsyncMock()) as client:
        with pytest.raises(ResponseTooLargeError):
            await client.get("https://docdoc.ru/doctor/a")

    assert received == [b"12345", b"67890"]
    assert response.is_closed


@pytest.mark.asyncio
async def test_response_at_size_limit_is_accepted(mock_client):
    async with HTTPClient(("docdoc.ru",), client=mock_client(lambda request: httpx.Response(200, content=b"12345")), max_response_bytes=5, url_validator=AsyncMock()) as client:
        response = await client.get("https://docdoc.ru/doctor/a")

    assert response.content == b"12345"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError, httpx.ProxyError, httpx.RemoteProtocolError])
@pytest.mark.parametrize("recover", [False, True])
async def test_network_retries_and_timeout_mapping(monkeypatch, mock_client, error_type, recover):
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

    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), retries=2, url_validator=validator) as client:
        if recover:
            assert (await client.get("https://docdoc.ru/doctor/a")).content == b"Reviews"
        else:
            with pytest.raises(SourceHTTPError) as error:
                await client.get("https://docdoc.ru/doctor/a")
            assert error.value.status_code == 504

    assert calls == validator.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.2, 0.4]


@pytest.mark.asyncio
async def test_other_request_errors_are_controlled_without_retries(mock_client):
    handler = Mock(side_effect=lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip")))
    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), url_validator=AsyncMock()) as client:
        with pytest.raises(SourceHTTPError) as error:
            await client.get("https://docdoc.ru/doctor/a")

    assert error.value.status_code == 502
    handler.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_owned_client_is_closed_after_success_or_error(monkeypatch, blocked):
    created = []
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        transport = httpx.MockTransport(lambda request: httpx.Response(403 if blocked else 200, text="Response"))
        created.append(real_client(transport=transport, **kwargs))
        return created[-1]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    async with HTTPClient(("docdoc.ru",), timeout=7, url_validator=AsyncMock()) as client:
        if blocked:
            with pytest.raises(SourceBlockedError):
                await client.get("https://docdoc.ru/doctor/a")
        else:
            await client.get("https://docdoc.ru/doctor/a")

    assert len(created) == 1
    assert created[0].is_closed
    assert created[0].follow_redirects is False
    assert created[0].timeout.read == 7
