"""Проверка доступа к СберЗдоровью и ПроДокторову с текущего сервера.

Для тестовых страниц (TEST_URL, PRODOCTOROV_TEST_URL) выполняется обычный HTTP-запрос,
а затем сбор через браузер Camoufox — так же, как это делает API. Браузер запускается
с временным профилем, чтобы не конфликтовать с работающим сервисом.

Запуск:
    python check_access.py
    docker compose exec web /app/venv/bin/python check_access.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

from dotenv import load_dotenv

from collectors.base import SOURCE_CONFIGS, Platform
from collectors.browser import close_browser, collect_with_browser
from collectors.http_client import HTTPClient
from collectors.prodoctorov import parse_prodoctorov_page
from collectors.sberzdorovie import parse_sber_page

load_dotenv()
CHECKS = (
    (Platform.SBERZDOROVIE, os.getenv("TEST_URL", "https://ekb.docdoc.ru/doctor/Mozgalina_Irina")),
    (Platform.PRODOCTOROV, os.getenv("PRODOCTOROV_TEST_URL", "https://prodoctorov.ru/ekaterinburg/vrach/248920-shakirov/")),
)


async def check_http(platform: Platform, url: str) -> str:
    async with HTTPClient(SOURCE_CONFIGS[platform].domains) as client:
        response = await client.get(url)
    result = parse_sber_page(response.text)[0] if platform == Platform.SBERZDOROVIE else parse_prodoctorov_page(response.text)
    return f"OK, отзывов: {len(result.reviews)}"


async def check_browser(platform: Platform, url: str) -> str:
    result = await collect_with_browser(url, platform)
    return f"OK, отзывов: {len(result.reviews)} — {result.title}"


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="check-access-", ignore_cleanup_errors=True) as profile_dir:
        os.environ["BROWSER_PROFILE_DIR"] = profile_dir
        try:
            for platform, url in CHECKS:
                print(f"\n=== {platform.value}: {url}")
                for name, check in (("HTTP", check_http), ("Браузер", check_browser)):
                    started = time.perf_counter()
                    try:
                        outcome = await check(platform, url)
                    except Exception as error:
                        outcome = f"FAIL {getattr(error, 'code', type(error).__name__)}: {error}"
                    print(f"{name}: {outcome} ({time.perf_counter() - started:.1f} c)")
        finally:
            await close_browser()


if __name__ == "__main__":
    asyncio.run(main())
