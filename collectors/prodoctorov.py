from __future__ import annotations

from lxml import html

from .base import CollectorResult, EmptyResponseError, Platform, SourceBlockedError, normalize_review
from .browser import collect_with_browser
from .http_client import HTTPClient


class ProdoctorovCollector:
    platform = Platform.PRODOCTOROV

    def __init__(self, http: HTTPClient):
        self.http = http

    async def collect(self, url: str, all_reviews: bool = False) -> CollectorResult:
        target = url.rstrip("/")
        if all_reviews and not target.endswith("/otzivi"):
            target += "/otzivi"
        try:
            response = await self.http.get(target)
        except SourceBlockedError:
            return await collect_with_browser(target, self.platform, all_reviews)
        try:
            return parse_prodoctorov_page(response.text, all_reviews)
        except EmptyResponseError:
            return await collect_with_browser(target, self.platform, all_reviews)


def parse_prodoctorov_page(content: str, all_reviews: bool = False) -> CollectorResult:
    document = html.fromstring(content)
    cards = document.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " b-review-card ")]')
    if not cards:
        raise EmptyResponseError("Prodoctorov review cards are missing")
    reviews = []
    for card in cards if all_reviews else cards[:20]:
        body = card.xpath('.//div[@itemprop="reviewBody"]')
        author = card.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " b-review-card__author-link ")]')
        published = card.xpath('.//div[@itemprop="datePublished"]')
        comment = card.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " b-review-card__comment ")]')
        rating = card.xpath('.//meta[@itemprop="ratingValue"]')
        if not comment or not "".join(comment[0].itertext()).strip():
            continue
        reviews.append(normalize_review({
            "id": body[0].get("data", "") if body else "",
            "name": "".join(author[0].itertext()) if author else "",
            "date": published[0].get("content", "") if published else "",
            "date_beauty": "".join(published[0].itertext()) if published else "",
            "message": "".join(comment[0].itertext()),
            "rating": rating[0].get("content", "0") if rating else "0",
        }, Platform.PRODOCTOROV))
    if not reviews:
        raise EmptyResponseError("Prodoctorov review cards contain no reviews")
    title = " ".join("".join(document.xpath("//title/text()")).split())
    return CollectorResult(title=title, reviews=reviews)
