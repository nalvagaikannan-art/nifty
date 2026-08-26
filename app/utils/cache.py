import time
import asyncio
import json
import inspect
import random
import logging
from functools import wraps
from typing import Any, Dict, Optional
from app.config import settings

logger = logging.getLogger(__name__)

_cache: Dict[str, tuple] = {}  # key: (timestamp, value)  — in-memory fallback

# Single-flight registry: concurrent callers for the same cache key await the
# same in-flight fetch instead of hammering NSE/Angel One/AI providers.
_inflight_lock = asyncio.Lock()
_inflight: Dict[str, asyncio.Task] = {}

# ── Optional Redis backend ───────────────────────────────────────────────
# Only activates when REDIS_URL is configured AND the `redis` package is
# installed. Without either, everything below silently falls back to the
# in-memory dict above — single-process behaviour is unchanged (see
# CODE_REVIEW.md #7). With it, multiple worker processes/instances share one
# cache instead of each keeping (and re-fetching NSE/AI data into) its own.
_redis_client = None
if settings.redis_url:
    try:
        import redis.asyncio as _redis_module
        _redis_client = _redis_module.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=2,
        )
        logger.info("async_cache: Redis backend configured (%s)", settings.redis_url)
    except ImportError:
        logger.warning(
            "REDIS_URL is set but the `redis` package isn't installed "
            "(pip install redis) — falling back to in-memory cache."
        )
    except Exception as e:
        logger.warning(f"Redis client init failed ({e}) — falling back to in-memory cache.")


def _json_default(obj):
    # Cached values here are already JSON-safe dicts/lists/primitives by the
    # time they reach this cache (market_analyzer._sanitize runs first), but
    # guard anyway rather than letting a stray type crash a cache write.
    return str(obj)

# Without this, `_cache` is a plain dict that only ever grows (every distinct
# args/kwargs combination adds a new entry, nothing ever removes an expired
# one) — a slow memory leak over a long-running process. We cap the size and
# opportunistically sweep expired entries on writes.
_MAX_CACHE_ENTRIES = 500
_DEFAULT_TTL_FOR_SWEEP = 300  # fallback assumed ttl for entries with no explicit ttl info


def _sweep_expired(ttl: int):
    now = time.time()
    expired = [k for k, (ts, _) in _cache.items() if now - ts > max(ttl, _DEFAULT_TTL_FOR_SWEEP)]
    for k in expired:
        _cache.pop(k, None)
    # Hard cap as a last resort if TTLs alone aren't keeping size down
    # (e.g. callers using very long TTLs with many distinct argument combos).
    if len(_cache) > _MAX_CACHE_ENTRIES:
        oldest_first = sorted(_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in oldest_first[: len(_cache) - _MAX_CACHE_ENTRIES]:
            _cache.pop(k, None)


def async_cache(ttl: int = None):
    """
    Async TTL cache with single-flight request deduplication.

    When several callers miss the same key at the same time, only the first
    caller executes the expensive function. Other callers await that task.
    This is especially important for market overview requests because both
    AI analysis and strategy endpoints can request the same snapshot together.
    """
    def decorator(func):
        sig_params = list(inspect.signature(func).parameters)
        has_self = sig_params and sig_params[0] in ("self", "cls")

        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Canonicalize positional/keyword arguments before building the key.
            # Without this, `get_full_market_overview("NIFTY")` and
            # `get_full_market_overview("NIFTY", expiry=None)` became TWO cache
            # entries, so the dashboard, analysis page and history collector
            # could all launch the same expensive market-data pipeline at once.
            try:
                bound = inspect.signature(func).bind(*args, **kwargs)
                bound.apply_defaults()
                key_parts = []
                for name, value in bound.arguments.items():
                    if has_self and name in ("self", "cls"):
                        continue
                    key_parts.append((name, repr(value)))
                key = f"{func.__qualname__}:{tuple(key_parts)}"
            except (TypeError, ValueError):
                key_args = args[1:] if has_self else args
                key = f"{func.__qualname__}:{key_args}:{kwargs}"
            effective_ttl = ttl or settings.cache_ttl

            async def _load_or_fetch():
                # Re-check Redis after becoming the single-flight owner. Another
                # worker may have populated the shared cache while we waited.
                if _redis_client is not None:
                    try:
                        cached = await _redis_client.get(key)
                        if cached is not None:
                            return json.loads(cached)
                    except Exception as e:
                        logger.warning(
                            f"Redis GET failed for {key}, falling through to live fetch: {e}"
                        )

                    result = await func(*args, **kwargs)
                    try:
                        await _redis_client.set(
                            key, json.dumps(result, default=_json_default), ex=effective_ttl
                        )
                    except Exception as e:
                        logger.warning(
                            f"Redis SET failed for {key} (result still returned): {e}"
                        )
                    return result

                # In-memory fallback.
                now = time.time()
                cached_entry = _cache.get(key)
                if cached_entry is not None:
                    ts, value = cached_entry
                    if now - ts < effective_ttl:
                        return value
                    _cache.pop(key, None)

                result = await func(*args, **kwargs)
                _cache[key] = (time.time(), result)

                if random.random() < 0.05:
                    _sweep_expired(effective_ttl)
                return result

            # Fast cache check before creating a task.
            if _redis_client is not None:
                try:
                    cached = await _redis_client.get(key)
                    if cached is not None:
                        return json.loads(cached)
                except Exception as e:
                    logger.warning(
                        f"Redis GET failed for {key}, falling through to single-flight fetch: {e}"
                    )
            else:
                now = time.time()
                cached_entry = _cache.get(key)
                if cached_entry is not None:
                    ts, value = cached_entry
                    if now - ts < effective_ttl:
                        return value
                    _cache.pop(key, None)

            async with _inflight_lock:
                task = _inflight.get(key)
                if task is None:
                    task = asyncio.create_task(_load_or_fetch())
                    _inflight[key] = task

            try:
                return await task
            finally:
                if task.done():
                    async with _inflight_lock:
                        if _inflight.get(key) is task:
                            _inflight.pop(key, None)

        return wrapper
    return decorator
