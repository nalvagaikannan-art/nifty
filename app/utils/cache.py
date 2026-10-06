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

# Single-flight registry.
#
# Completed cache entries remain shared across foreground/background callers.
# In-flight work is priority-aware so a foreground HTTP request never joins a
# background history-collector task. A background caller may still join an
# already-running foreground task.
_inflight_lock = asyncio.Lock()
_inflight: Dict[tuple, asyncio.Task] = {}


def _singleflight_scope() -> str:
    """
    Keep foreground and background in-flight work separate.

    The import is intentionally lazy: cache.py is imported by modules that are
    also involved in AngelOne initialization, so importing angel_one at module
    load time could create a circular import.
    """
    try:
        from app.services.angel_one import (
            get_angel_call_priority,
            ANGEL_PRIORITY_BACKGROUND,
        )
        if get_angel_call_priority() == ANGEL_PRIORITY_BACKGROUND:
            return "background"
    except Exception:
        # Cache must remain independent of broker-priority implementation.
        pass
    return "foreground"

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
    # SAFETY FIX 2026-09-11:
    # Do NOT expire cache entries using the TTL of the caller that triggered
    # this opportunistic sweep. The in-memory cache stores entries created by
    # functions with different TTLs (for example, historical data = 1800s
    # while other data may use 30s/60s).
    #
    # Previously, a short-TTL caller could randomly trigger this 5% sweep and
    # delete a still-valid long-TTL historical entry. That caused unnecessary
    # ONE_DAY Angel One requests and contributed to broker rate limiting.
    #
    # Individual cache lookups already enforce each function's own
    # `effective_ttl` before returning an entry, so expiration remains correct
    # without globally applying the current caller's TTL to every entry.
    #
    # Keep this sweep responsible only for the hard memory cap. Expired entries
    # are removed naturally when their own cache key is accessed.
    del ttl

    if len(_cache) > _MAX_CACHE_ENTRIES:
        oldest_first = sorted(_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in oldest_first[: len(_cache) - _MAX_CACHE_ENTRIES]:
            _cache.pop(k, None)


def async_cache(ttl: int = None, failure_ttl: int = None):
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

            # Keep completed market-overview cache separate for foreground and
            # background callers. A background collector result must never
            # overwrite/poison the foreground HTTP overview cache.
            overview_cache_scoped = (
                func.__qualname__.endswith(
                    "MarketAnalyzer.get_full_market_overview"
                )
                or func.__qualname__.endswith(
                    "MarketAnalyzer._get_full_market_overview_cached"
                )
            )
            cache_scope = (
                _singleflight_scope()
                if overview_cache_scoped
                else "shared"
            )
            cache_key = (
                f"{key}:scope={cache_scope}"
                if overview_cache_scoped
                else key
            )

            async def _load_or_fetch():
                # Re-check Redis after becoming the single-flight owner. Another
                # worker may have populated the shared cache while we waited.
                if _redis_client is not None:
                    try:
                        cached = await _redis_client.get(cache_key)
                        if cached is not None:
                            return json.loads(cached)
                    except Exception as e:
                        logger.warning(
                            f"Redis GET failed for {key}, falling through to live fetch: {e}"
                        )

                    result = await func(*args, **kwargs)
                    try:
                        await _redis_client.set(
                            cache_key, json.dumps(result, default=_json_default), ex=effective_ttl
                        )
                    except Exception as e:
                        logger.warning(
                            f"Redis SET failed for {key} (result still returned): {e}"
                        )
                    return result

                # In-memory fallback.
                now = time.time()
                cached_entry = _cache.get(cache_key)
                if cached_entry is not None:
                    # Cache entries normally contain (timestamp, value).
                    # New failure-aware entries contain
                    # (timestamp, value, entry_ttl).
                    # Keeping support for the old 2-item format makes this
                    # change backward-compatible with entries created before
                    # a process restart or future cache-format changes.
                    if len(cached_entry) == 3:
                        ts, value, entry_ttl = cached_entry
                    else:
                        ts, value = cached_entry
                        entry_ttl = effective_ttl

                    if now - ts < entry_ttl:
                        return value

                    _cache.pop(cache_key, None)

                result = await func(*args, **kwargs)

                # SAFETY FIX 2026-09-11:
                # Cache broker/API failures only briefly. This prevents an
                # Angel One rate-limit response from being treated as valid
                # for the entire normal success TTL.
                #
                # We intentionally detect the standard DataFetcher failure
                # shape instead of treating every falsy result as a failure.
                # Empty lists/dicts from unrelated cached functions should
                # not unexpectedly change their existing TTL semantics.
                is_intraday_failure = (
                    failure_ttl is not None
                    and isinstance(result, dict)
                    and result.get("available") is False
                )

                entry_ttl = failure_ttl if is_intraday_failure else effective_ttl

                # Store the TTL with the entry so each cache item uses its
                # own expiry instead of the caller's current/default TTL.
                _cache[cache_key] = (time.time(), result, entry_ttl)

                if random.random() < 0.05:
                    _sweep_expired(effective_ttl)
                return result

            # Fast cache check before creating a task.
            if _redis_client is not None:
                try:
                    cached = await _redis_client.get(cache_key)
                    if cached is not None:
                        return json.loads(cached)
                except Exception as e:
                    logger.warning(
                        f"Redis GET failed for {key}, falling through to single-flight fetch: {e}"
                    )
            else:
                now = time.time()
                cached_entry = _cache.get(cache_key)

                if func.__qualname__.endswith(
                    "DataFetcher._get_historical_prices_base"
                ):
                    if cached_entry is None:
                        logger.info(
                            "HIST_CACHE DIAG MISS key=%s cache_size=%d",
                            cache_key,
                            len(_cache),
                        )
                    else:
                        ts, _value = cached_entry[:2]
                        logger.info(
                            "HIST_CACHE DIAG ENTRY key=%s age=%.3fs ttl=%s cache_size=%d",
                            cache_key,
                            now - ts,
                            effective_ttl,
                            len(_cache),
                        )

                if cached_entry is not None:
                    # SAFETY FIX 2026-09-11:
                    # Cache entries can contain either:
                    #   (timestamp, value)            -> legacy format
                    #   (timestamp, value, entry_ttl) -> per-entry TTL format
                    #
                    # The old code unpacked only two values here.
                    # That caused HTTP 500 when failure_ttl stored the
                    # third value (entry_ttl) in the cache.
                    #
                    # Use the TTL belonging to THIS cache entry so that
                    # different cache functions cannot expire each other's
                    # entries using the wrong caller TTL.
                    if len(cached_entry) == 3:
                        ts, value, entry_ttl = cached_entry
                    else:
                        ts, value = cached_entry
                        entry_ttl = effective_ttl

                    if now - ts < entry_ttl:
                        if func.__qualname__.endswith(
                            "DataFetcher._get_historical_prices_base"
                        ):
                            logger.info(
                                "HIST_CACHE DIAG HIT age=%.3fs ttl=%.3fs",
                                now - ts,
                                entry_ttl,
                            )
                        return value

                    _cache.pop(cache_key, None)

                    if func.__qualname__.endswith(
                        "DataFetcher._get_historical_prices_base"
                    ):
                        logger.info(
                            "HIST_CACHE DIAG EXPIRED age=%.3fs ttl=%.3fs",
                            now - ts,
                            entry_ttl,
                        )

            # Priority-aware single-flight:
            #
            # Foreground callers must NEVER inherit/join a background owner's
            # ContextVar. That was causing HTTP strategy requests to execute
            # Angel candle calls with priority=background.
            #
            # Background callers may join an existing foreground task because
            # that task already has the correct foreground execution context.
            scope = _singleflight_scope()

            async with _inflight_lock:
                task = None
                inflight_key = None

                if scope == "background":
                    # Prefer an already-running foreground computation.
                    foreground_key = (key, "foreground")
                    background_key = (key, "background")

                    task = _inflight.get(foreground_key)
                    if task is not None:
                        inflight_key = foreground_key
                    else:
                        task = _inflight.get(background_key)
                        if task is not None:
                            inflight_key = background_key
                else:
                    # Foreground callers never join background work.
                    inflight_key = (key, "foreground")
                    task = _inflight.get(inflight_key)

                if task is None:
                    task = asyncio.create_task(_load_or_fetch())
                    _inflight[inflight_key] = task

            try:
                return await task
            finally:
                if task.done():
                    async with _inflight_lock:
                        if inflight_key is not None and _inflight.get(inflight_key) is task:
                            _inflight.pop(inflight_key, None)

        return wrapper
    return decorator
