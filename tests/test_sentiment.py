from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import main
import sentiment_service
from app.cache import AsyncTTLCache
from app.limiter import FixedWindowRateLimiter


@pytest.fixture(autouse=True)
def sentiment_config(monkeypatch):
    monkeypatch.setattr(main, "API_AUTH_ENABLED", False)
    monkeypatch.setattr(main, "API_KEY", "")
    monkeypatch.setattr(main, "AI_API_KEY", "test-key")
    monkeypatch.setattr(main, "AI_API_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(main, "AI_MODEL", "qwen/qwen3-30b-a3b:free")
    main.app.state.cache = AsyncTTLCache(60, 10)
    main.app.state.blocked_cache = AsyncTTLCache(60, 10)
    main.app.state.rate_limiter = FixedWindowRateLimiter(100, 60)


@pytest.fixture
def api_client():
    transport = httpx.ASGITransport(app=main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_sentiment_endpoint_returns_single_sentiment(monkeypatch, api_client):
    check = AsyncMock(return_value="positive")
    monkeypatch.setattr(main, "check_review_sentiment", check)

    async with api_client as client:
        response = await client.post("/api/v1/checkSentiment", json={"review": "Врач помог"})

    assert response.status_code == 200
    assert response.json() == {"sentiment": "positive"}
    check.assert_awaited_once_with("Врач помог")


@pytest.mark.asyncio
async def test_sentiment_endpoint_returns_batch_sentiments(monkeypatch, api_client):
    check = AsyncMock(return_value={"results": [
        {"id": "positive", "sentiment": "positive"},
        {"id": "negative", "sentiment": "negative"},
    ]})
    monkeypatch.setattr(main, "check_batch_reviews_sentiment", check)
    reviews = [
        {"id": "positive", "text": "Отличный врач"},
        {"id": "negative", "text": "Ужасный сервис"},
    ]

    async with api_client as client:
        response = await client.post("/api/v1/checkSentiment", json={"reviews": reviews})

    assert response.status_code == 200
    assert response.json()["results"] == [
        {"id": "positive", "sentiment": "positive"},
        {"id": "negative", "sentiment": "negative"},
    ]
    check.assert_awaited_once_with(reviews)


@pytest.mark.asyncio
async def test_sentiment_endpoint_rejects_invalid_json(api_client):
    async with api_client as client:
        response = await client.post(
            "/api/v1/checkSentiment",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_json"}


@pytest.mark.parametrize("url, expected", [
    ("https://api.example.com/v1", "https://api.example.com/v1"),
    ("https://api.example.com/v1/", "https://api.example.com/v1"),
    ("https://api.example.com/v1/chat/completions", "https://api.example.com/v1"),
    (None, None),
])
def test_normalize_provider_api_url(url, expected):
    assert sentiment_service.normalize_base_url(url) == expected


@pytest.mark.asyncio
async def test_sentiment_service_uses_configured_model(monkeypatch):
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"positive": "positive", "negative": "negative"}'))]
    )
    create = AsyncMock(return_value=completion)
    monkeypatch.setattr(sentiment_service, "AI_MODEL", "qwen/qwen3-30b-a3b:free")
    monkeypatch.setattr(sentiment_service.client.chat.completions, "create", create)
    reviews = [
        {"id": "positive", "text": "Врач помог"},
        {"id": "negative", "text": "Врач не помог"},
    ]

    result = await sentiment_service.check_batch_reviews_sentiment(reviews)

    assert result == {"results": [
        {"id": "positive", "sentiment": "positive"},
        {"id": "negative", "sentiment": "negative"},
    ]}
    assert create.call_args.kwargs["model"] == "qwen/qwen3-30b-a3b:free"
    assert create.call_args.kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_live_openrouter_model_classifies_sentiment(live):
    if not live:
        pytest.skip("Use pytest -live to call the configured AI provider")
    original_key = sentiment_service.AI_API_KEY
    original_url = sentiment_service.AI_API_URL
    original_model = sentiment_service.AI_MODEL
    if not original_key or not original_url or not original_model:
        pytest.skip("AI_API_KEY, AI_API_URL and AI_MODEL are required")

    result = await sentiment_service.check_batch_reviews_sentiment([
        {"id": "positive", "text": "Врач внимательно выслушал, помог, лечение дало отличный результат."},
        {"id": "negative", "text": "Врач грубил, не выслушал, лечение не помогло. Никому не рекомендую."},
    ])

    assert "results" in result, result
    sentiments = {item["id"]: item["sentiment"] for item in result["results"]}
    assert sentiments["positive"] == "positive", result
    assert sentiments["negative"] == "negative", result


@pytest.mark.asyncio
async def test_sentiment_service_returns_error_for_invalid_ai_json(monkeypatch):
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))]
    )
    create = AsyncMock(return_value=completion)
    monkeypatch.setattr(sentiment_service.client.chat.completions, "create", create)

    result = await sentiment_service.check_batch_reviews_sentiment([
        {"id": "1", "text": "Отзыв"},
    ])

    assert result["error"] == "AI response was not valid JSON"
    assert result["raw_response"] == "not-json"
