"""Проверка доступа к SberHealth/docdoc через curl_cffi.

Запуск:
    python check_access.py
"""

from __future__ import annotations

import json
import re
import os
import time

from dotenv import load_dotenv

load_dotenv()
TEST_URL = os.getenv("TEST_URL", "https://ekb.docdoc.ru/doctor/Brant_Ekaterina")
PRODOCTOROV_TEST_URL = os.getenv("PRODOCTOROV_TEST_URL", "https://prodoctorov.ru/ekaterinburg/vrach/248920-shakirov/")

BLOCK_MARKERS = ("captcha", "recaptcha", "доступ ограничен", "access denied",
                 "проверка, что вы не робот", "вы заблокированы", "too many requests")


def inspect_html(content: str) -> dict[str, object]:
    lowered = content.lower()
    next_data = "__next_data__" in lowered
    review_markers = ("b-review-card", "reviewbody", "reviewtext", "отзывы")
    marker_count = sum(marker in lowered for marker in review_markers)
    challenge = "servicepipe.tech/static/checkjs/" in lowered or (not content.strip() and "servicepipe" in lowered)
    return {
        "bytes": len(content.encode("utf-8")),
        "next_data": next_data,
        "review_markers": marker_count,
        "servicepipe_challenge": challenge,
        "block_marker": check_block(content),
    }


def print_probe(name: str, response) -> bool:
    info = inspect_html(response.text)
    print(f"{name}: HTTP {response.status_code}, {info}")
    return response.status_code == 200 and (bool(info["next_data"]) or int(info["review_markers"]) >= 2) and not info["servicepipe_challenge"]


def check_block(content: str) -> str | None:
    lowered = content.lower()
    for marker in BLOCK_MARKERS:
        if marker in lowered:
            return marker
    return None


def run_curl_cffi() -> None:
    from curl_cffi import requests

    print(f"\n=== curl_cffi: {TEST_URL} ===")
    started = time.perf_counter()
    try:
        response = requests.get(TEST_URL, impersonate="chrome", timeout=30)
    except Exception as error:
        print(f"FAIL: запрос не выполнен: {error}")
        return
    elapsed = time.perf_counter() - started
    print(f"HTTP {response.status_code}, {len(response.content)} байт, {elapsed:.1f} c")
    if response.status_code != 200:
        print(f"BLOCKED: статус {response.status_code}")
        print(f"Начало ответа:\n{response.text[:500]}")
        return
    has_next_data = "__NEXT_DATA__" in response.text
    print(f"OK: __NEXT_DATA__ присутствует = {has_next_data}")
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', response.text, re.DOTALL)
    if not match:
        marker = check_block(response.text)
        print(f"BLOCKED: маркер '{marker}'" if marker else "Нет __NEXT_DATA__ в ответе")
        return

    try:
        data = json.loads(match.group(1))
        state = data["props"]["pageProps"]["preloadedState"]
        doctor = state["doctorPage"]["doctor"]
        doctor_reviews = state.get("doctorReviews", {})
        reviews = doctor_reviews.get("reviewsForSeo") or doctor_reviews.get("reviews")
        if reviews is None:
            reviews = doctor.get("reviewsForSeo", [])

        if not reviews:
            marker = check_block(response.text)
            print(f"BLOCKED: отзывов нет, найден маркер '{marker}'" if marker else "WARN: структура загружена, но отзывов нет")
            print("Доступные поля doctorReviews:", sorted(doctor_reviews.keys()))
            return

        print(f"OK: получены реальные отзывы: {len(reviews)}")
        for index, review in enumerate(reviews[:5], start=1):
            name = review.get("name") or review.get("author") or "без имени"
            message = review.get("message") or review.get("text") or review.get("body") or ""
            rating = review.get("rating") or review.get("ratingValue") or "-"
            date = review.get("date") or review.get("dateBeauty") or ""
            print(f"  {index}. [{rating}/5] {name}, {date}")
            print(f"     {str(message).strip()[:300]}")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"WARN: не удалось разобрать данные отзывов: {error}")


def run_prodoctorov_probes() -> None:
    import httpx
    import requests as plain_requests
    from curl_cffi import requests as curl_requests

    print(f"\n=== Prodoctorov lightweight probes: {PRODOCTOROV_TEST_URL} ===")
    browser_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Referer": "https://prodoctorov.ru/",
        "Upgrade-Insecure-Requests": "1",
    }
    probes = [
        ("requests", lambda: plain_requests.get(PRODOCTOROV_TEST_URL, headers=browser_headers, timeout=20, allow_redirects=False)),
        ("httpx", lambda: httpx.get(PRODOCTOROV_TEST_URL, headers=browser_headers, timeout=20, follow_redirects=False)),
        ("curl_cffi chrome", lambda: curl_requests.get(PRODOCTOROV_TEST_URL, headers=browser_headers, impersonate="chrome", timeout=20, allow_redirects=False)),
        ("curl_cffi chrome131", lambda: curl_requests.get(PRODOCTOROV_TEST_URL, headers=browser_headers, impersonate="chrome131", timeout=20, allow_redirects=False)),
    ]
    for name, request in probes:
        started = time.perf_counter()
        try:
            response = request()
            elapsed = time.perf_counter() - started
            print(f"{name} elapsed={elapsed:.2f}s")
            if print_probe(name, response):
                print(f"WORKING_LIGHTWEIGHT_TRANSPORT={name}")
                return
        except Exception as error:
            print(f"{name}: FAIL {type(error).__name__}: {error}")
    print("NO_LIGHTWEIGHT_TRANSPORT_WORKED")


def main() -> None:
    run_curl_cffi()
    run_prodoctorov_probes()


if __name__ == "__main__":
    main()
