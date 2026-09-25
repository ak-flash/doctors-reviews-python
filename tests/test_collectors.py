import json
from unittest.mock import Mock

import httpx
import pytest

from collectors.base import CollectorResult, EmptyResponseError, Platform, SourceBlockedError, SourceHTTPError, normalize_rating, normalize_url
from collectors.http_client import HTTPClient
from collectors.prodoctorov import ProdoctorovCollector
from collectors.sberzdorovie import SberZdorovieCollector
from collectors.sberzdorovie import parse_sber_page


async def noop_validator(url):
    return None


@pytest.mark.parametrize(("value", "expected"), [("5", 50), ("4.5", 45), ({"value": 5}, 50), ({"value": 10, "label": "Отлично"}, 100), (None, 0)])
def test_normalize_rating(value, expected):
    assert normalize_rating(value) == expected


def test_normalize_url_whitelist():
    assert normalize_url("https://docdoc.ru/doctor/a#x", Platform.SBERZDOROVIE) == "https://docdoc.ru/doctor/a"
    with pytest.raises(Exception):
        normalize_url("http://localhost/x", Platform.SBERZDOROVIE)


@pytest.mark.asyncio
async def test_sber_parser(mock_client):
    payload = {"props": {"pageProps": {"preloadedState": {"doctorPage": {"doctor": {"id": 1, "reviewsForSeo": [{"id": 1, "name": " Ann ", "isoDate": "2025-01-02T12:00:00Z", "date": "2 января", "text": " Good ", "rating": {"value": 10, "label": "Отлично"}}]}}}}}}
    transport = mock_client(lambda request: httpx.Response(200, text=f'<title>Doctor</title><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'))
    async with HTTPClient(("docdoc.ru",), client=transport, url_validator=noop_validator) as client:
        result = await SberZdorovieCollector(client).collect("https://docdoc.ru/doctor/a")
    assert result.title == "Doctor"
    assert result.reviews[0].message == "Good"
    assert result.reviews[0].date == "2025-01-02"
    assert result.reviews[0].date_beauty == "2 января"
    assert result.reviews[0].rating == 100


@pytest.mark.asyncio
async def test_prodoctorov_parser(mock_client, monkeypatch):
    content = '<title>Отзывы врача</title><div class="b-review-card"><div itemprop="reviewBody" data="r1"></div><a class="b-review-card__author-link">Анна</a><div itemprop="datePublished" content="2025-01-02">2 января</div><div class="b-review-card__comment">Внимательный врач</div><meta itemprop="ratingValue" content="5"></div>'
    transport = mock_client(lambda request: httpx.Response(200, text=content))
    monkeypatch.setattr("collectors.prodoctorov.collect_with_browser", pytest.fail)
    async with HTTPClient(("prodoctorov.ru",), client=transport, url_validator=noop_validator) as client:
        result = await ProdoctorovCollector(client).collect("https://prodoctorov.ru/doctor/a")
    assert result.title == "Отзывы врача"
    assert result.reviews[0].id == "r1"
    assert result.reviews[0].name == "Анна"
    assert result.reviews[0].message == "Внимательный врач"
    assert result.reviews[0].rating == 50


@pytest.mark.asyncio
async def test_prodoctorov_browser_fallback(monkeypatch, mock_client):
    transport = mock_client(lambda request: httpx.Response(200, text='<script src="https://servicepipe.tech/static/checkjs/x.js"></script>'))
    expected = CollectorResult(title="Doctor")
    called = []

    async def fallback(url, platform, all_reviews):
        called.append((url, platform, all_reviews))
        return expected

    monkeypatch.setattr("collectors.prodoctorov.collect_with_browser", fallback)
    async with HTTPClient(("prodoctorov.ru",), client=transport, url_validator=noop_validator) as client:
        result = await ProdoctorovCollector(client).collect("https://prodoctorov.ru/doctor/a", True)

    assert result == expected
    assert called == [("https://prodoctorov.ru/doctor/a/otzivi", Platform.PRODOCTOROV, True)]


@pytest.mark.asyncio
async def test_block_detection(mock_client):
    transport = mock_client(lambda request: httpx.Response(403, text="captcha"))
    async with HTTPClient(("docdoc.ru",), client=transport, url_validator=noop_validator) as client:
        with pytest.raises(SourceBlockedError):
            await client.get("https://docdoc.ru/doctor/a")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["reviewsForSeo", "reviews"])
async def test_sber_current_next_data_ignores_captcha_configuration(mock_client, field):
    state = {
        "doctorPage": {"doctor": {"id": 1}},
        "doctorReviews": {field: [{"id": 1, "name": "Анна", "text": "Внимательный врач"}], "totalReviewCount": 1},
        "captchaEnabled": True,
    }
    payload = {"props": {"pageProps": {"preloadedState": state}}}
    content = f'<title>Отзывы врача</title><script src="/recaptcha.js"></script><script id="__NEXT_DATA__">{json.dumps(payload, ensure_ascii=False)}</script>'
    transport = mock_client(lambda request: httpx.Response(200, text=content))

    async with HTTPClient(("docdoc.ru",), client=transport, url_validator=noop_validator) as client:
        result = await SberZdorovieCollector(client).collect("https://docdoc.ru/doctor/a")

    assert result.title == "Отзывы врача"
    assert result.reviews[0].name == "Анна"
    assert result.reviews[0].message == "Внимательный врач"


@pytest.mark.asyncio
@pytest.mark.parametrize("all_reviews", [False, True])
async def test_more_reviews_uses_shared_session_and_validates_url(mock_client, all_reviews):
    source_url = "https://ekb.docdoc.ru/doctor/a"
    state = {
        "doctorPage": {"doctor": {"id": 42}},
        "doctorReviews": {"reviewsForSeo": [{"id": 1, "text": "First"}], "totalReviewCount": 2},
    }
    payload = {"props": {"pageProps": {"preloadedState": state}}}
    validated = []

    async def validator(url):
        validated.append(url)

    def handle(request):
        assert str(request.url) in validated
        if request.url.path == "/doctor/a":
            return httpx.Response(200, text=f'<script id="__NEXT_DATA__">{json.dumps(payload)}</script>', headers={"Set-Cookie": "session=shared; Domain=.docdoc.ru; Path=/"})
        assert request.url.host == "docdoc.ru"
        assert request.url.path == "/doctors/moreReviews"
        assert dict(request.url.params) == {"isHiddenDoctor": "0", "offset": "1", "doctorId": "42", "reviewsSort": "new"}
        assert request.headers["Cookie"] == "session=shared"
        assert request.headers["Referer"] == source_url
        assert request.headers["Origin"] == "https://ekb.docdoc.ru"
        assert request.headers["Sec-Fetch-Mode"] == "cors"
        return httpx.Response(200, json={"reviews": [{"id": 2, "text": "Second"}]})

    handler = Mock(side_effect=handle)
    async with HTTPClient(("docdoc.ru",), client=mock_client(handler), url_validator=validator) as client:
        result = await SberZdorovieCollector(client).collect(source_url, all_reviews=all_reviews)

    assert [review.message for review in result.reviews] == (["First", "Second"] if all_reviews else ["First"])
    assert handler.call_count == len(validated) == (2 if all_reviews else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("response, error", [
    (httpx.Response(429, text="Too many requests"), SourceBlockedError),
    (httpx.Response(200, text="not JSON"), EmptyResponseError),
    (httpx.Response(200, json={"reviews": None}), EmptyResponseError),
])
async def test_more_reviews_errors_are_not_silently_ignored(mock_client, response, error):
    state = {
        "doctorPage": {"doctor": {"id": 42}},
        "doctorReviews": {"reviewsForSeo": [{"text": "First"}], "totalReviewCount": 2},
    }
    payload = {"props": {"pageProps": {"preloadedState": state}}}

    def handler(request):
        if request.url.path == "/doctor/a":
            return httpx.Response(200, text=f'<script id="__NEXT_DATA__">{json.dumps(payload)}</script>')
        return response

    transport = mock_client(handler)
    async with HTTPClient(("docdoc.ru",), client=transport, url_validator=noop_validator) as client:
        with pytest.raises(error):
            await SberZdorovieCollector(client).collect("https://docdoc.ru/doctor/a", all_reviews=True)


def test_sber_not_found_page_is_reported_as_404():
    payload = {"page": "/404", "props": {"pageProps": {}}}
    content = f'<title>404 — Страница не найдена</title><script id="__NEXT_DATA__">{json.dumps(payload)}</script>'

    with pytest.raises(SourceHTTPError) as error:
        parse_sber_page(content)

    assert error.value.status_code == 404


@pytest.mark.parametrize(("html_title", "seo", "expected"), [
    ("", {"head": {"title": " Врач –  отзывы | СберЗдоровье "}}, "Врач – отзывы | СберЗдоровье"),
    ("Заголовок страницы", {"head": {"title": "SEO"}}, "Заголовок страницы"),
    ("", ["unexpected"], ""),
])
def test_sber_title_falls_back_to_seo_data(html_title, seo, expected):
    payload = {"props": {"pageProps": {"seo": seo, "preloadedState": {"doctorPage": {"doctor": {"id": 1}}, "doctorReviews": {"reviewsForSeo": []}}}}}
    content = f'<title>{html_title}</title><script id="__NEXT_DATA__">{json.dumps(payload, ensure_ascii=False)}</script>'

    result, _, _ = parse_sber_page(content)

    assert result.title == expected
