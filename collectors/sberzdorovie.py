from __future__ import annotations

import json
from urllib.parse import urlencode, urlparse

from lxml import html

from .base import CollectorResult, EmptyResponseError, Platform, Review, SourceBlockedError, SourceHTTPError, normalize_review
from .browser import collect_with_browser
from .http_client import HTTPClient


class SberZdorovieCollector:
    platform = Platform.SBERZDOROVIE

    def __init__(self, http: HTTPClient):
        self.http = http

    async def collect(self, url: str, all_reviews: bool = False) -> CollectorResult:
        try:
            response = await self.http.get(url)
        except SourceBlockedError:
            return await collect_with_browser(url, self.platform, all_reviews)
        try:
            result, doctor_id, total_reviews = parse_sber_page(response.text)
        except EmptyResponseError:
            return await collect_with_browser(url, self.platform, all_reviews)
        if all_reviews and doctor_id and len(result.reviews) < total_reviews:
            result.reviews.extend(await _load_more_reviews(self.http, doctor_id, len(result.reviews), str(response.url)))
        return result


def parse_sber_page(content: str) -> tuple[CollectorResult, int | str | None, int]:
    document = html.fromstring(content)
    scripts = document.xpath('//script[@id="__NEXT_DATA__"]/text()')
    if not scripts:
        raise EmptyResponseError("SberZdorovie __NEXT_DATA__ is missing")
    try:
        data = json.loads(scripts[0])
    except ValueError as exc:
        raise EmptyResponseError("SberZdorovie review data is missing") from exc
    if isinstance(data, dict) and data.get("page") in {"/404", "/_error"}:
        raise SourceHTTPError("SberZdorovie doctor page not found", status_code=404)
    try:
        page_props = data["props"]["pageProps"]
        state = page_props["preloadedState"]
        doctor = state["doctorPage"]["doctor"]
        doctor_reviews = state.get("doctorReviews", {})
        raw_reviews = doctor_reviews.get("reviewsForSeo") or doctor_reviews.get("reviews")
        if raw_reviews is None:
            raw_reviews = doctor.get("reviewsForSeo", [])
        if not isinstance(raw_reviews, list):
            raise ValueError("Invalid review list")
        doctor_id = doctor.get("id")
        total_reviews = int(doctor_reviews.get("totalReviewCount") or len(raw_reviews))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise EmptyResponseError("SberZdorovie review data is missing") from exc
    reviews = [normalize_review(item, Platform.SBERZDOROVIE) for item in raw_reviews if isinstance(item, dict)]
    title = " ".join("".join(document.xpath("//title/text()")).split()) or _seo_title(page_props)
    return CollectorResult(title=title, reviews=reviews), doctor_id, total_reviews


def _seo_title(page_props: dict) -> str:
    # Next.js may fill <title> only after hydration; the server-rendered SEO data already contains it.
    seo = page_props.get("seo")
    head = seo.get("head") if isinstance(seo, dict) else None
    title = head.get("title") if isinstance(head, dict) else None
    return " ".join(title.split()) if isinstance(title, str) else ""


async def _load_more_reviews(http: HTTPClient, doctor_id: int | str, offset: int, source_url: str) -> list[Review]:
    query = urlencode({"isHiddenDoctor": 0, "offset": offset, "doctorId": doctor_id, "reviewsSort": "new"})
    url = f"https://docdoc.ru/doctors/moreReviews?{query}"
    source = urlparse(source_url)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru,en;q=0.9",
        "Referer": source_url,
        "Origin": f"{source.scheme}://{source.netloc}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site" if source.hostname and (source.hostname == "docdoc.ru" or source.hostname.endswith(".docdoc.ru")) else "cross-site",
        "X-Experiments": "gtm-test:1,GTM-ZAPIS:1,GTM-DOCTOR-FULL:1,gtm-test-2:0,GTM-505:1,GTM-507:1",
    }

    response = await http.get(url, headers=headers)
    try:
        payload = response.json()
        raw_reviews = payload["reviews"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmptyResponseError("SberZdorovie additional review data is missing") from exc
    if not isinstance(raw_reviews, list):
        raise EmptyResponseError("SberZdorovie additional review data is invalid")
    return [normalize_review(item, Platform.SBERZDOROVIE) for item in raw_reviews if isinstance(item, dict)]
