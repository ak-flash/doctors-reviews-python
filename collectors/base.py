from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse
import ipaddress

from lxml import etree, html
from pydantic import BaseModel, ConfigDict, Field


class Platform(str, Enum):
    SBERZDOROVIE = "sberzdorovie"
    PRODOCTOROV = "prodoctorov"


class CollectorError(Exception):
    status_code = 502
    code = "collector_error"

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class InvalidURLError(CollectorError):
    status_code = 400
    code = "invalid_url"


class UnsupportedPlatformError(CollectorError):
    status_code = 400
    code = "unsupported_platform"


class SourceBlockedError(CollectorError):
    status_code = 503
    code = "source_blocked"


class EmptyResponseError(CollectorError):
    status_code = 502
    code = "empty_response"


class SourceHTTPError(CollectorError):
    code = "source_http_error"


class Review(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str = ""
    date: str = ""
    date_beauty: str = ""
    message: str = ""
    rating: int = Field(default=0, ge=0)
    source: str = ""


class CollectorResult(BaseModel):
    title: str = ""
    reviews: list[Review] = Field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"title": self.title, "reviews": [review.model_dump() for review in self.reviews]}


@dataclass(frozen=True)
class SourceConfig:
    platform: Platform
    domains: tuple[str, ...]


SOURCE_CONFIGS = {
    Platform.SBERZDOROVIE: SourceConfig(Platform.SBERZDOROVIE, ("docdoc.ru", "sberhealth.ru")),
    Platform.PRODOCTOROV: SourceConfig(Platform.PRODOCTOROV, ("prodoctorov.ru",)),
}


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_date(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    for parser in (datetime.fromisoformat, date.fromisoformat):
        try:
            return parser(text.replace("Z", "+00:00")).date().isoformat()
        except (TypeError, ValueError):
            pass
    return text


def normalize_rating(value: Any) -> int:
    if isinstance(value, dict):
        try:
            number = float(str(value.get("value", "")).replace(",", ".").strip())
        except (TypeError, ValueError):
            return 0
        return max(0, int(round(number * 10)))
    try:
        number = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return 0
    if 0 < number <= 5:
        number *= 10
    return max(0, int(round(number)))


def normalize_review(raw: dict[str, Any], source: Platform | str) -> Review:
    published = raw.get("isoDate") or raw.get("date") or raw.get("datePublished", "")
    beauty = raw.get("date_beauty") or raw.get("dateBeauty") or raw.get("dateText") or raw.get("date") or published
    text = raw.get("message", raw.get("text", raw.get("body", "")))
    author = raw.get("name", raw.get("author", raw.get("authorName", "")))
    return Review(
        id=normalize_text(raw.get("id", raw.get("review_id", ""))),
        name=normalize_text(author),
        date=normalize_date(published),
        date_beauty=normalize_text(beauty),
        message=normalize_text(text),
        rating=normalize_rating(raw.get("rating", raw.get("ratingValue", 0))),
        source=source.value if isinstance(source, Platform) else str(source),
    )


def normalize_url(url: str, platform: Platform) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise InvalidURLError("URL is missing or too long")
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise InvalidURLError("URL must use http or https")
    if parsed.port not in (None, 80, 443):
        raise InvalidURLError("URL port is not allowed")
    host = parsed.hostname.lower().rstrip(".")
    config = SOURCE_CONFIGS.get(platform)
    if not config or not any(host == domain or host.endswith("." + domain) for domain in config.domains):
        raise InvalidURLError("URL domain is not allowed for selected platform")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise InvalidURLError("URL address is not public")
    except ValueError:
        pass
    return parsed._replace(fragment="").geturl()


def is_blocked_content(status_code: int, content: bytes | str) -> bool:
    if status_code in {403, 429, 503}:
        return True
    text = content.decode("utf-8", "ignore") if isinstance(content, bytes) else content
    lowered = text.lower()
    markers = ("captcha", "recaptcha", "проверка, что вы не робот", "access denied", "доступ ограничен", "вы заблокированы", "too many requests", "verify you are human")
    challenge_script = "servicepipe.tech/static/checkjs/"
    if not any(marker in lowered for marker in markers) and challenge_script not in lowered:
        return False
    try:
        document = html.fromstring(text)
    except (etree.ParserError, ValueError):
        return True
    lowered = " ".join(document.xpath("//text()[not(ancestor::script or ancestor::style or ancestor::template)]")).lower()
    lowered = " ".join(lowered.split())
    if any(marker in lowered for marker in markers):
        return True
    return not lowered and any(challenge_script in source.lower() for source in document.xpath("//script/@src"))
