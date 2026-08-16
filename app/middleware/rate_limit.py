"""
Basic per-IP rate limiting for /api/* routes.

Without this, anyone who can reach the server could trigger unlimited NSE
scraping (accelerating IP blocks) and unlimited AI provider calls (direct
cost). Default is a simple in-memory sliding-window limiter — good enough for
a single-process deployment, but each worker process tracks its own counts
independently, so effective limit = N workers x requests_per_minute.

When settings.redis_url is configured (and the `redis` package is
installed), this switches to a Redis-backed fixed-window counter shared by
every worker/instance — see CODE_REVIEW.md #7. Falls back to in-memory
automatically if Redis is unreachable at request time.
"""
import time
import logging
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, requests_per_minute: int = 60, applies_to_prefix: str = "/api/"):
        super().__init__(app)
        self.limit = requests_per_minute
        self.window_seconds = 60
        self.applies_to_prefix = applies_to_prefix
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

        self._redis = None
        if settings.redis_url:
            try:
                import redis.asyncio as redis_module
                self._redis = redis_module.from_url(
                    settings.redis_url, decode_responses=True, socket_timeout=2,
                )
                logger.info("RateLimitMiddleware: Redis backend configured (%s)", settings.redis_url)
            except ImportError:
                logger.warning(
                    "REDIS_URL is set but the `redis` package isn't installed "
                    "(pip install redis) — rate limiting falls back to in-memory (per-worker)."
                )
            except Exception as e:
                logger.warning(f"Redis client init failed ({e}) — rate limiting falls back to in-memory.")

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(self.applies_to_prefix):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        limited = await self._is_limited_redis(client_ip) if self._redis is not None else None
        if limited is None:  # Redis unavailable/unset — in-memory fallback
            limited = self._is_limited_memory(client_ip)

        if limited:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "detail": f"Too many requests — limit is {self.limit} per {self.window_seconds}s.",
                },
                headers={"Retry-After": str(self.window_seconds)},
            )

        return await call_next(request)

    def _is_limited_memory(self, client_ip: str) -> bool:
        now = time.time()
        hits = self._hits[client_ip]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()
        if len(hits) >= self.limit:
            return True
        hits.append(now)
        return False

    async def _is_limited_redis(self, client_ip: str) -> Optional[bool]:
        """Fixed-window counter (INCR + EXPIRE) — simpler than a sliding-window
        sorted-set and good enough for this purpose. Returns None (meaning
        "fall back to in-memory") if Redis itself is unreachable right now,
        so a Redis blip degrades gracefully instead of taking rate limiting
        down entirely."""
        try:
            window = int(time.time() // self.window_seconds)
            key = f"ratelimit:{client_ip}:{window}"
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, self.window_seconds)
            return count > self.limit
        except Exception as e:
            logger.warning(f"Redis rate-limit check failed ({e}) — falling back to in-memory for this request.")
            return None
