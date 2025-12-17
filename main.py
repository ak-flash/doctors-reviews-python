import asyncio
import fastapi,uvicorn
import json
from typing import List
from pydantic import BaseModel
from enum import Enum
# Camoufox here!
from camoufox.async_api import AsyncCamoufox
import os
# import httpx  # Removed as we use sentiment_service
from dotenv import load_dotenv
import logging
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from contextlib import asynccontextmanager
import hashlib
from datetime import datetime
from sentiment_service import check_batch_reviews_sentiment, check_review_sentiment

load_dotenv()
AI_API_URL = os.getenv("AI_API_URL")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL")  # Например: distilbert-base-uncased-finetuned-sst-2-english
SAVE_SCREENSHOT = os.getenv("SAVE_SCREENSHOT", "true").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

class Platform(str, Enum):
    SBERZDOROVIE = "sberzdorovie"
    PRODOCTOROV = "prodoctorov"


class Review(BaseModel):
    id: str
    name: str
    date: str
    date_beauty: str
    message: str
    rating: int
    source: str


def modify_url_for_platform(url: str, platform: Platform, all_reviews: bool = False) -> str:
    if platform == Platform.PRODOCTOROV:
        # Убираем trailing slash если есть
        url = url.rstrip('/')
        # Проверяем, есть ли уже /otzivi в URL для загрузки всех отзывов
        if not url.endswith('/otzivi') and all_reviews:
            # Добавляем параметр для загрузки всех отзывов
            url = f"{url}/otzivi"
        
    return url


# Глобальный браузер через lifespan
@asynccontextmanager
async def lifespan(app):
    # Camoufox automatically handles Playwright start/stop
    # We use persistent_context=True to keep session data in user_data_dir
    print("Starting Camoufox browser...")
    async with AsyncCamoufox(
        headless=False,
        humanize=True,  # Включаем имитацию человеческого поведения курсора
        user_data_dir="data",
        persistent_context=True,
        args=['--no-sandbox', '--disable-setuid-sandbox']
    ) as context:
        app.state.browser_context = context
        
        # --- Warm-up: Открываем главные страницы для "прогрева" сессии ---
        logging.info("Warming up browser: checking background tabs...")
        
        # Проверяем, открыты ли уже эти страницы (например, восстановлены из сессии)
        pages = context.pages
        docdoc_open = any("docdoc.ru" in p.url for p in pages)
        prodoctorov_open = any("prodoctorov.ru" in p.url for p in pages)
        
        try:
            # Получаем первую пустую страницу (если она есть), чтобы не плодить окна
            empty_page = None
            for p in pages:
                if p.url == "about:blank":
                    empty_page = p
                    break

            if not docdoc_open:
                logging.info("Opening SberZdorovie (docdoc.ru)...")
                # Используем пустую страницу или создаем новую
                if empty_page:
                    p1 = empty_page
                    empty_page = None # Использовали
                else:
                    p1 = await context.new_page()
                
                await p1.goto("https://docdoc.ru", timeout=60000, wait_until="domcontentloaded")
            
            if not prodoctorov_open:
                logging.info("Opening ProDoctorov...")
                # Если осталась пустая страница (вряд ли, но вдруг), используем её
                if empty_page:
                    p2 = empty_page
                else:
                    p2 = await context.new_page()
                    
                await p2.goto("https://prodoctorov.ru", timeout=60000, wait_until="domcontentloaded")
                
        except Exception as e:
            logging.error(f"Warm-up error: {e}")
            
        logging.info("Browser warm-up complete.")
        
        yield

# async def get_browser_context(): ... (Removed)


app = fastapi.FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get('/favicon.ico')
async def favicon():
    return FileResponse('favicon.ico')
            

@app.get("/api/v1/getReviews")
async def run_playwright(url: str = None, platform: Platform = None, all_reviews: bool = False):
    if not url:
        return {
            "error": "URL parameter missing",
            "details": "Please provide URL in query parameters, e.g.: /?url=https://docdoc.ru/doctor/SomeDoctor"
        }
    if not platform:
        return {
            "error": "Platform missing",
            "details": "Please provide platform in query parameters: platform=sberzdorovie or platform=prodoctorov"
        }
    return await fetch(url, platform, all_reviews)

@app.post("/api/v1/checkSentiment")
async def sentiment_route(request: Request):
    """
    Endpoint for sentiment analysis.
    Supports two formats:
    1. Single review: {"review": "..."} -> {"sentiment": "..."}
    2. Batch reviews: {"reviews": [{"id": "1", "text": "..."}, ...]} -> {"results": [{"id": "1", "sentiment": "..."}, ...]}
    """
    try:
        data = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON in request body", "details": str(e)}
        )

    if not AI_API_KEY or not AI_API_URL or not AI_MODEL:
        return {"error": "AI_API_KEY, AI_API_URL, AI_MODEL not set in .env file"}

    # 1. Batch processing
    if "reviews" in data and isinstance(data["reviews"], list):
        reviews_data = data["reviews"]
        if not reviews_data:
            return {"results": []}

        # Run one request to API
        return await check_batch_reviews_sentiment(reviews_data)

    # 2. Single review processing (Legacy)
    review = data.get("review")
    if not review:
        return {"error": "Review text (review) or list of reviews (reviews) missing"}
    
    try:
        sentiment = await check_review_sentiment(review)
        return {"sentiment": sentiment}
    except Exception as e:
        logging.error(f"Sentiment Analysis Error: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Sentiment analysis error",
                "details": str(e)
            }
        )



async def fetch(url: str, platform: Platform, all_reviews: bool = False):
    # Модифицируем URL в зависимости от платформы
    url = modify_url_for_platform(url, platform, all_reviews)
    
    # Используем глобальный контекст
    browser_context = app.state.browser_context
    
    # Всегда создаем новую вкладку для новой задачи
    # Это гарантирует, что мы не помешаем фоновым вкладкам (docdoc/prodoctorov)
    page = await browser_context.new_page()

    try:
        # Увеличиваем таймаут до 60 секунд и добавляем паузу после загрузки
        await page.goto(url, timeout=60000, wait_until='networkidle')
        await asyncio.sleep(5) # Даем скриптам на странице время на инициализацию
        title = await page.title()
        reviews = await parse_reviews(platform, page, all_reviews)
        # --- Сохраняем скриншот ---
        screenshot_path = None
        if SAVE_SCREENSHOT:
            os.makedirs("screenshots", exist_ok=True)
            url_hash = hashlib.md5(url.encode()).hexdigest()
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
            screenshot_path = f"screenshots/{timestamp}_{url_hash}.png"
            await page.screenshot(path=screenshot_path, full_page=True)
        
        # Всегда закрываем вкладку после работы
        await page.close()
                
        result = {
            "title": title,
            "reviews": [review.model_dump() for review in reviews]
        }
        if screenshot_path:
            result["screenshot"] = screenshot_path
        return result
    except Exception as e:
        # Если была ошибка, закрываем вкладку
        try:
            await page.close()
        except Exception:
            pass
        return {
            "error": "Page load error",
            "details": str(e)
        }


async def parse_reviews(platform: Platform, page, all_reviews: bool = False) -> List[Review]:
    reviews: List[Review] = []
    
    if platform == Platform.SBERZDOROVIE:
        # Ждем появления скрипта с данными (обходим возможные спиннеры/проверки на бота)
        try:
            # Script тег не видимый, поэтому ждем state='attached'
            await page.wait_for_selector("#__NEXT_DATA__", state="attached", timeout=20000)
        except Exception:
            return []

        # Получаем данные из скрипта
        next_data = await page.evaluate('''() => {
            const script = document.getElementById('__NEXT_DATA__');
            return script ? script.textContent : null;
        }''')

        if not next_data:
            return []
        try:
            data = json.loads(next_data)
            if 'props' in data and 'pageProps' in data['props']:
                raw_reviews = data['props']['pageProps']['preloadedState']['doctorPage']['doctor']['reviewsForSeo']
                for review in raw_reviews:
                    reviews.append(Review(
                        id=str(review.get('id', '')),
                        name=review.get('name', ''),
                        date=review.get('isoDate', ''),
                        date_beauty=review.get('date', ''),
                        message=review.get('text', ''),
                        rating=str(int(review.get('rating', {}).get('value', 0) * 10)),
                        source=platform
                    ))
        except json.JSONDecodeError:
            reviews = []
    
    elif platform == Platform.PRODOCTOROV:
        # Ждем загрузки основного контента
        try:
            await page.wait_for_selector(".b-review-card", timeout=15000)
        except Exception:
            return []
        # Получаем все отзывы одним запросом
        reviews_data = await page.evaluate(f'''() => {{
            const reviews = [];
            const cards = Array.from(document.querySelectorAll('.b-review-card'));
            const cardsToProcess = {'cards.slice(0, 20)' if not all_reviews else 'cards'};
            
            cardsToProcess.forEach(card => {{
                const reviewBody = card.querySelector('div[itemprop="reviewBody"]');
                const authorLink = card.querySelector('.b-review-card__author-link');
                const dateElem = card.querySelector('div[itemprop="datePublished"]');
                const messageElem = card.querySelector('.b-review-card__comment');
                const ratingElem = card.querySelector('meta[itemprop="ratingValue"]');
                
                if (messageElem && messageElem.textContent.trim()) {{
                    reviews.push({{
                        id: reviewBody ? reviewBody.getAttribute('data') : '',
                        name: authorLink ? authorLink.textContent.replace(/\\s+/g, ' ').trim() : '',
                        date: dateElem ? dateElem.getAttribute('content') : '',
                        date_beauty: dateElem ? dateElem.textContent.replace(/\\s+/g, ' ').trim() : '',
                        message: messageElem.textContent.replace(/\\s+/g, ' ').trim(),
                        rating: ratingElem ? ratingElem.getAttribute('content') : '0'
                    }});
                }}
            }});
            return reviews;
        }}''')
        
        # Преобразуем полученные данные в объекты Review
        for review_data in reviews_data:
            reviews.append(Review(
                id=review_data['id'],
                name=review_data['name'],
                date=review_data['date'],
                date_beauty=review_data['date_beauty'],
                message=review_data['message'],
                rating=review_data['rating'],
                source=platform
            ))
    
    # for review in reviews:
    #     if AI_API_KEY:
    #         review.sentiment = await check_review_sentiment(review.message)
    
    return reviews


if __name__ == "__main__":

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        reload=False,
        port=9000
    )