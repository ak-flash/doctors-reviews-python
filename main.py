import asyncio
import fastapi,uvicorn
import json
from typing import List, Dict, Optional
from pydantic import BaseModel
from enum import Enum
# patchright here!
from patchright.async_api import async_playwright
import os
import httpx
from dotenv import load_dotenv
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
import pathlib

load_dotenv()
AI_API_URL = os.getenv("AI_API_URL")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL")  # Например: distilbert-base-uncased-finetuned-sst-2-english

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


async def fetch(url: str, platform: Platform, all_reviews: bool = False):
    # Модифицируем URL в зависимости от платформы
    url = modify_url_for_platform(url, platform, all_reviews)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="data",
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        page = await browser.new_page()
        try:
            # Увеличиваем таймаут и добавляем ожидание загрузки сети
            await page.goto(url, timeout=30000, wait_until='networkidle')

            # Получаем заголовок страницы
            title = await page.title()
            
            # Парсим отзывы
            reviews = await parse_reviews(platform, page, all_reviews)
            
            await browser.close()
            return {
                "title": title,
                "reviews": [review.dict() for review in reviews]
            }
        except Exception as e:
            await browser.close()
            return {
                "error": "Ошибка при загрузке страницы",
                "details": str(e)
            }
    


async def parse_reviews(platform: Platform, page, all_reviews: bool = False) -> List[Review]:
    reviews: List[Review] = []
    
    if platform == Platform.SBERZDOROVIE:
        # Получаем данные из скрипта
        next_data = await page.evaluate('''() => {
            const script = document.getElementById('__NEXT_DATA__');
            return script ? script.textContent : null;
        }''')

        if not next_data:
            return {
                "error": "Ошибка получения отзывов",
                "details": "Пожалуйста, проверьте корректность URL"
            }
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
        await page.wait_for_selector(".b-review-card", timeout=5000)
        
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
    
    return reviews


async def check_review_sentiment(text: str) -> str:
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
        # "HTTP-Referer": "<YOUR_SITE_URL>",  # если нужно
        # "X-Title": "<YOUR_SITE_NAME>",      # если нужно
    }
    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "user",
                "content": f"Определи, является ли этот отзыв положительным или отрицательным. Ответь только 'positive' или 'negative'. Отзыв: {text}"
            }
        ]
    }
    # Гарантируем, что /chat/completions всегда в конце URL
    api_url = AI_API_URL.rstrip('/')
    if not api_url.endswith('/chat/completions'):
        api_url = f"{api_url}/chat/completions"
    async with httpx.AsyncClient() as client:
        response = await client.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        logging.info(f"AI API response for text '{text[:30]}...': {result}")
        # Парсим ответ
        try:
            content = result["choices"][0]["message"]["content"].strip().lower()
            if "negative" in content:
                return "negative"
            elif "positive" in content:
                return "positive"
            else:
                return content
        except Exception as e:
            logging.error(f"Ошибка парсинга ответа AI: {e}")
            return "unknown"


app = fastapi.FastAPI()

@app.get("/api/v1/getReviews")
async def run_playwright(url: str = None, platform: Platform = None, all_reviews: bool = False):
    if not url:
        return {
            "error": "Не указан параметр url",
            "details": "Пожалуйста, укажите URL в параметрах запроса, например: /?url=https://docdoc.ru/doctor/SomeDoctor"
        }
    if not platform:
        return {
            "error": "Не указана платформа",
            "details": "Пожалуйста, укажите платформу в параметрах запроса: platform=sberzdorovie или platform=prodoctorov"
        }
    return await fetch(url, platform, all_reviews)

@app.post("/api/v1/checkSentiment")
async def sentiment_route(request: Request):
    """
    Эндпоинт для проверки тональности одного отзыва.
    Ожидает JSON: {"review": "..."}
    """
    try:
        data = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "Некорректный JSON в теле запроса", "details": str(e)}
        )
    review = data.get("review")
    if not review:
        return {"error": "Не передан текст отзыва (review)"}
    if not AI_API_KEY or not AI_API_URL or not AI_MODEL:
        return {"error": "AI_API_KEY, AI_API_URL, AI_MODEL не установлены в .env файле"}
    sentiment = await check_review_sentiment(review)
    return {"sentiment": sentiment}

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = pathlib.Path("index.html")
    if not index_path.exists():
        return HTMLResponse("<b>index.html не найден</b>", status_code=500)
    html = index_path.read_text(encoding="utf-8")
    api_key_short = AI_API_KEY[:4] + "..." if AI_API_KEY else "не задан"
    html = html.replace("{{AI_API_URL}}", AI_API_URL or "не задан")
    html = html.replace("{{AI_MODEL}}", AI_MODEL or "не задан")
    html = html.replace("{{AI_API_KEY}}", api_key_short)
    return HTMLResponse(content=html)

@app.get('/favicon.ico')
async def favicon():
    return FileResponse('favicon.ico')

if __name__ == "__main__":

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        reload=False,
        port=9000
    )