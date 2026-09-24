from unittest.mock import Mock

import httpx
import pytest
from curl_cffi import requests as curl_requests
from curl_cffi.requests.headers import Headers


def pytest_load_initial_conftests(early_config, parser, args):
    if "-live" in args:
        args[args.index("-live")] = "--live"


def pytest_addoption(parser):
    parser.addoption("-L", "--live", action="store_true", default=False, help="run tests with real AI provider requests")


@pytest.fixture
def live(request):
    return request.config.getoption("live")


@pytest.fixture(autouse=True)
def forbid_real_browser(monkeypatch):
    async def forbidden(**options):
        pytest.fail("Unit tests must inject a fake Camoufox factory, not launch a real browser")

    monkeypatch.setattr("collectors.browser._create_camoufox", forbidden)
    monkeypatch.setattr("collectors.browser._browser", None)


@pytest.fixture
def curl_session_factory():
    def factory(handler):
        session = Mock(spec=curl_requests.Session)
        cookies = httpx.Cookies()

        def get(url, **kwargs):
            request = httpx.Request("GET", url, headers=kwargs["headers"])
            cookies.set_cookie_header(request)
            response = handler(request)
            response.request = request
            try:
                cookies.extract_cookies(response)
                result = curl_requests.Response()
                result.status_code = response.status_code
                result.headers = Headers(response.headers.multi_items())
                result.url = str(request.url)
                for chunk in response.iter_bytes():
                    kwargs["content_callback"](chunk)
                return result
            finally:
                response.close()

        session.get.side_effect = get
        return session

    return factory
