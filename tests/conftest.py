import httpx
import pytest


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
def mock_client():
    def factory(handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory
