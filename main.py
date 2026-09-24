import asyncio
import secrets
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

import fastapi
import uvicorn
from dotenv import load_dotenv
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.cache import AsyncTTLCache
from app.limiter import FixedWindowRateLimiter
from app import security
from collectors.base import CollectorError, EmptyResponseError, InvalidURLError, Platform, SOURCE_CONFIGS, SourceBlockedError
from collectors.http_client import HTTPClient
from collectors.browser import close_browser, stats as browser_stats
from collectors.prodoctorov import ProdoctorovCollector
from collectors.sberzdorovie import SberZdorovieCollector
from sentiment_service import check_batch_reviews_sentiment, check_review_sentiment

load_dotenv()
AI_API_URL = os.getenv("AI_API_URL")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL")
API_AUTH_ENABLED = os.getenv("API_AUTH_ENABLED", "false").lower() in {"1", "true", "yes"}
API_KEY = os.getenv("API_KEY", "")
SENTIMENT_MAX_BODY_BYTES = int(os.getenv("SENTIMENT_MAX_BODY_BYTES", "1048576"))
CACHE_TTL = float(os.getenv("CACHE_TTL_SECONDS", "900"))
BLOCKED_CACHE_TTL = float(os.getenv("BLOCKED_CACHE_TTL_SECONDS", "30"))
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "256"))
MAX_RESULT_REVIEWS = int(os.getenv("MAX_RESULT_REVIEWS", "200"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "30"))
RATE_WINDOW = float(os.getenv("RATE_WINDOW_SECONDS", "60"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_COLLECTIONS", "10"))
CORS_ORIGINS = [item.strip() for item in os.getenv("CORS_ORIGINS", "").split(",") if item.strip()]
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(), logging.FileHandler("app.log", encoding="utf-8")])
logger = logging.getLogger(__name__)
metrics = {"requests": 0, "cache_hits": 0, "fallbacks": 0, "errors": 0}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    app.state.cache = AsyncTTLCache(CACHE_TTL, CACHE_MAX_ENTRIES)
    app.state.blocked_cache = AsyncTTLCache(BLOCKED_CACHE_TTL, CACHE_MAX_ENTRIES)
    app.state.semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    app.state.rate_limiter = FixedWindowRateLimiter(RATE_LIMIT, RATE_WINDOW)
    try:
        yield
    finally:
        await close_browser()


app = fastapi.FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-API-Key"])


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if path not in {"/", "/favicon.ico", "/health"}:
        if API_AUTH_ENABLED and not API_KEY:
            return JSONResponse(status_code=500, content={"error": "server_configuration_error"})
        supplied = request.headers.get("X-API-Key", "")
        if API_AUTH_ENABLED and (not supplied or not secrets.compare_digest(supplied, API_KEY)):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        client = request.client.host if request.client else "unknown"
        allowed, retry = await request.app.state.rate_limiter.check(f"{client}:{supplied}")
        if not allowed:
            return JSONResponse(status_code=429, headers={"Retry-After": str(max(1, int(retry + 0.999)))}, content={"error": "rate_limited"})
    metrics["requests"] += 1
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", encoding="utf-8") as file:
        return HTMLResponse(file.read())


@app.get("/favicon.ico")
async def favicon():
    return FileResponse("favicon.ico")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def get_metrics():
    return {**metrics, **browser_stats}


def modify_url_for_platform(url: str, platform: Platform, all_reviews: bool = False) -> str:
    normalized = security.validate_url_shape(url, platform)
    if platform == Platform.PRODOCTOROV and all_reviews and not normalized.rstrip("/").endswith("/otzivi"):
        return normalized.rstrip("/") + "/otzivi"
    return normalized


def cache_key(url: str, platform: Platform, all_reviews: bool = False) -> str:
    return f"{platform.value}:{url}:{'all' if all_reviews else 'page'}"


async def fetch(url: str, platform: Platform, all_reviews: bool = False):
    started = time.perf_counter()
    target = modify_url_for_platform(url, platform, all_reviews)
    key = cache_key(target, platform, all_reviews)
    cached = await app.state.cache.get(key)
    if cached is not None:
        metrics["cache_hits"] += 1
        return cached
    blocked = await app.state.blocked_cache.get(key)
    if blocked is not None:
        metrics["cache_hits"] += 1
        return JSONResponse(status_code=blocked["status_code"], content=blocked["content"])
    try:
        async with app.state.semaphore:
            domains = SOURCE_CONFIGS[platform].domains
            async with HTTPClient(domains) as client:
                collector = SberZdorovieCollector(client) if platform == Platform.SBERZDOROVIE else ProdoctorovCollector(client)
                result = await collector.collect(target, all_reviews)
        if MAX_RESULT_REVIEWS >= 0:
            result.reviews = result.reviews[:MAX_RESULT_REVIEWS]
        public = result.public()
        await app.state.cache.set(key, public)
        logger.info("collection elapsed_ms=%.1f", (time.perf_counter() - started) * 1000)
        return public
    except CollectorError as error:
        metrics["errors"] += 1
        content = {"error": error.code, "details": str(error)}
        if isinstance(error, SourceBlockedError):
            await app.state.blocked_cache.set(key, {"status_code": error.status_code, "content": content}, BLOCKED_CACHE_TTL)
        return JSONResponse(status_code=error.status_code, content=content)
    except Exception:
        metrics["errors"] += 1
        logger.exception("collection failed")
        return JSONResponse(status_code=502, content={"error": "collection_error"})


@app.get("/api/v1/getReviews")
async def get_reviews(url: str | None = None, platform: Platform | None = None, all_reviews: bool = False):
    if not url:
        return JSONResponse(status_code=400, content={"error": "invalid_url", "details": "URL parameter missing"})
    if not platform:
        return JSONResponse(status_code=400, content={"error": "unsupported_platform", "details": "Platform parameter missing"})
    try:
        return await fetch(url, platform, all_reviews)
    except InvalidURLError as error:
        return JSONResponse(status_code=400, content={"error": error.code, "details": str(error)})


@app.post("/api/v1/checkSentiment")
async def sentiment_route(request: Request):
    if request.headers.get("content-length") and int(request.headers["content-length"]) > SENTIMENT_MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"error": "request_too_large"})
    try:
        body = await request.body()
        if len(body) > SENTIMENT_MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"error": "request_too_large"})
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    if not AI_API_KEY or not AI_API_URL or not AI_MODEL:
        return {"error": "sentiment_unavailable"}
    if isinstance(data.get("reviews"), list):
        return await check_batch_reviews_sentiment(data["reviews"])
    if not data.get("review"):
        return {"error": "review_missing"}
    try:
        return {"sentiment": await check_review_sentiment(data["review"])}
    except Exception:
        metrics["errors"] += 1
        return JSONResponse(status_code=500, content={"error": "sentiment_error"})


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", reload=False, port=9000)
