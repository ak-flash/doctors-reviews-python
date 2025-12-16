import os
import json
import logging
import asyncio
from typing import List, Dict, Any
from dotenv import load_dotenv
from openai import AsyncOpenAI, APIError, RateLimitError

load_dotenv()

AI_API_URL = os.getenv("AI_API_URL")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL")

# Настройка клиента OpenAI
# OpenRouter требует базовый URL заканчивающийся на /v1, но библиотека openai сама добавляет /chat/completions к base_url?
# Нет, base_url в openai должен указывать на корень API, например https://openrouter.ai/api/v1
# Если AI_API_URL у нас https://openrouter.ai/api/v1, то все ок.
# Если там /chat/completions, надо обрезать.

base_url = AI_API_URL
if base_url and base_url.endswith("/chat/completions"):
    base_url = base_url.replace("/chat/completions", "")
if base_url and base_url.endswith("/"):
    base_url = base_url.rstrip("/")

client = AsyncOpenAI(
    base_url=base_url,
    api_key=AI_API_KEY,
    max_retries=3  # Библиотека сама умеет делать ретраи
)

async def check_batch_reviews_sentiment(reviews_data: List[dict]) -> dict:
    """
    Отправляет пакет отзывов в AI одним запросом и получает JSON-ответ.
    """
    if not reviews_data:
        return {"results": []}

    # Подготовка данных для промпта
    prompt_items = []
    for item in reviews_data:
        # Убираем лишние переносы строк, чтобы не ломать JSON-подобную структуру в тексте
        text = item.get("text", "").replace("\n", " ").replace('"', "'")
        prompt_items.append(f'{{"id": "{item.get("id")}", "text": "{text}"}}')
    
    reviews_json_str = "[\n" + ",\n".join(prompt_items) + "\n]"

    system_prompt = "You are a sentiment analysis assistant. You respond ONLY with valid JSON."
    user_prompt = f"""
Analyze the sentiment of the reviews provided below.
For each review, determine if it is 'positive', 'negative', or 'neutral'.

Input Data:
{reviews_json_str}

Output Format:
Return a JSON object where keys are the review IDs and values are the sentiment strings.
Example:
{{
  "1": "positive",
  "2": "negative"
}}

Do not add any markdown formatting (like ```json). Just the raw JSON string.
"""

    try:
        completion = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            extra_headers={
                "HTTP-Referer": "https://doctors-reviews.local", # Optional
                "X-Title": "Doctors Reviews App", # Optional
            }
        )

        content_str = completion.choices[0].message.content
        logging.info(f"AI Batch Response: {content_str}")

        # Попытка распарсить JSON из ответа
        try:
            # Иногда модели добавляют ```json ... ```, убираем их на всякий случай
            if "```" in content_str:
                content_str = content_str.replace("```json", "").replace("```", "").strip()
            
            sentiment_map = json.loads(content_str)
        except json.JSONDecodeError:
            logging.error(f"Failed to parse AI JSON response: {content_str}")
            return {"error": "AI response was not valid JSON", "raw_response": content_str}

        # Формируем итоговый список результатов
        results = []
        for item in reviews_data:
            item_id = str(item.get("id")) # Приводим к строке для матчинга ключей
            sentiment = sentiment_map.get(item_id, "unknown")
            results.append({"id": item_id, "sentiment": sentiment})
        
        return {"results": results}

    except RateLimitError as e:
        logging.error(f"AI API Rate Limit Error: {e}")
        return {"error": "Rate limit exceeded. Please try again later.", "details": str(e)}
    except APIError as e:
        logging.error(f"AI API Error: {e}")
        return {"error": f"AI API returned error", "details": str(e)}
    except Exception as e:
        logging.error(f"Batch Sentiment Analysis Error: {e}")
        return {"error": "Internal error during batch analysis", "details": str(e)}

async def check_review_sentiment(text: str) -> str:
    """
    Обертка для проверки одного отзыва через batch-механизм (для унификации).
    """
    result = await check_batch_reviews_sentiment([{"id": "single", "text": text}])
    if "results" in result and result["results"]:
        return result["results"][0]["sentiment"]
    return "error"
