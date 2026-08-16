import time
import json
import inspect
import random
import logging
from functools import wraps
from typing import Any, Dict, Optional
from app.config import settings

logger = logging.getLogger(__name__)

_cache: Dict[str, tuple] = {}  # key: (timestamp, value)  — in-memory fallback

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
    def decorator(func):
        sig_params = list(inspect.signature(func).parameters)
        has_self = sig_params and sig_params[0] in ("self", "cls")

        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Exclude `self`/`cls` from the cache key: its default repr includes
            # a memory address, so a bound method's key would differ every time
            # a new instance is created (e.g. one per HTTP request via FastAPI
            # `Depends`), making the cache never hit even for identical calls.
            key_args = args[1:] if has_self else args
            key = f"{func.__qualname__}:{key_args}:{kwargs}"
            effective_ttl = ttl or settings.cache_ttl

            if _redis_client is not None:
                try:
                    cached = await _redis_client.get(key)
                    if cached is not None:
                        return json.loads(cached)
                except Exception as e:
                    logger.warning(f"Redis GET failed for {key}, falling through to live fetch: {e}")

                result = await func(*args, **kwargs)
                try:
                    await _redis_client.set(key, json.dumps(result, default=_json_default), ex=effective_ttl)
                except Exception as e:
                    logger.warning(f"Redis SET failed for {key} (result still returned): {e}")
                return result

            # In-memory fallback (single-process only — see module docstring).
            now = time.time()
            if key in _cache:
                ts, value = _cache[key]
                if now - ts < effective_ttl:
                    return value
            result = await func(*args, **kwargs)
            _cache[key] = (now, result)
            # Sweep occasionally rather than on every write (cheap amortized cost).
            if random.random() < 0.05:
                _sweep_expired(effective_ttl)
            return result
        return wrapper
    return decorator
