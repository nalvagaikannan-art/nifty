"""
Shared cache for AIEngine.analyze_market() results.

BUG FIX (2026-08-25): /api/analysis/ai/{symbol} AND /api/strategy/recommend/
{symbol} each independently called ai.analyze_market(market_data) — i.e. TWO
separate LLM API round trips (Gemini/OpenAI/Deepseek) for the same symbol,
fired in parallel by the dashboard's ltRefresh(), every single refresh. That
extra LLM call sat on top of /api/strategy/recommend's own heavy pipeline
(confluence engine, market regime, trade levels, risk engine, strike/Greeks
computation for every strike) and was the biggest single reason that route
occasionally ran long enough to hit a proxy/connection timeout — which shows
up client-side as a truncated/empty body ("Unexpected end of JSON input"),
not a clean error.

This module makes analyze_market's result a shared, single-flight, TTL
cache keyed on (symbol, expiry, market_data timestamp) — the timestamp only
changes when get_full_market_overview actually re-fetched, so within one
market snapshot both routes reuse the SAME AI call instead of duplicating
it. Whichever route asks first pays for the LLM call; the other awaits the
same in-flight request instead of starting its own.
"""
import asyncio
import time
import logging
import math
from typing import Dict

from app.config import settings
from app.exceptions import AIProviderError
from app.services.ai_engine import AIEngine

logger = logging.getLogger(__name__)

_cache: Dict[str, tuple] = {}          # key -> (timestamp, result)
_failure_cache: Dict[str, tuple] = {}   # key -> (timestamp, exception)
_inflight: Dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()
_MAX_ENTRIES = 200


def _make_key(symbol: str, expiry: str, market_data: dict) -> str:
    # AI is an explanation/validation layer, not the live decision engine.
    # Do not key it on the live-calculation timestamp: that timestamp changes
    # on every market refresh and would defeat the shared AI TTL cache.
    ttl = max(1, int(settings.analysis_cache_ttl))
    bucket = math.floor(time.time() / ttl)
    return f"{symbol}:{expiry or ''}:ai_bucket:{bucket}"


def _sweep():
    total = len(_cache) + len(_failure_cache)
    if total <= _MAX_ENTRIES:
        return

    combined = list(_cache.items()) + list(_failure_cache.items())
    oldest = sorted(combined, key=lambda kv: kv[1][0])[: total - _MAX_ENTRIES]

    for k, _ in oldest:
        _cache.pop(k, None)
        _failure_cache.pop(k, None)


async def get_ai_analysis(ai: AIEngine, symbol: str, expiry: str, market_data: dict) -> dict:
    """Drop-in replacement for `await ai.analyze_market(market_data)` that
    dedupes identical calls (same symbol/expiry/snapshot) across routes and
    across concurrent requests, reusing the result for
    settings.analysis_cache_ttl seconds."""
    key = _make_key(symbol, expiry, market_data)
    now = time.time()

    cached = _cache.get(key)
    if cached is not None and now - cached[0] < settings.analysis_cache_ttl:
        return cached[1]

    # Do not repeatedly hit an exhausted provider.  The route already has a
    # safe rule-engine fallback for AIProviderError, so a cached failure is
    # re-raised locally without another Gemini request.
    failed = _failure_cache.get(key)
    if failed is not None and now - failed[0] < settings.analysis_cache_ttl:
        raise failed[1]

    async with _lock:
        # Re-check both caches after acquiring the lock so concurrent requests
        # do not create duplicate provider calls.
        now = time.time()

        cached = _cache.get(key)
        if cached is not None and now - cached[0] < settings.analysis_cache_ttl:
            return cached[1]

        failed = _failure_cache.get(key)
        if failed is not None and now - failed[0] < settings.analysis_cache_ttl:
            raise failed[1]

        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(ai.analyze_market(market_data))
            _inflight[key] = task

    try:
        result = await task
    except AIProviderError as exc:
        _failure_cache[key] = (time.time(), exc)
        _sweep()
        raise
    finally:
        async with _lock:
            if _inflight.get(key) is task:
                _inflight.pop(key, None)

    _cache[key] = (time.time(), result)
    _failure_cache.pop(key, None)
    _sweep()
    return result
