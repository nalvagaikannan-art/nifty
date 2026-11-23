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


def _quality_fingerprint(market_data: dict) -> tuple:
    quality = market_data.get("option_chain_quality")
    if not isinstance(quality, dict) or not quality:
        meta = market_data.get("pcr_quality")
        quality = meta.get("chain_quality") if isinstance(meta, dict) else None

    valid = bool(market_data.get("option_chain_valid", False))
    rows = int(market_data.get("option_chain_rows", 0) or 0)
    if not isinstance(quality, dict) or not quality:
        return ("unreported", valid, rows)

    available = quality.get("available") is True
    oi_ok = available and quality.get("oi_usable") is True
    vol_ok = available and quality.get("volume_usable") is True

    def pct(field):
        value = quality.get(field)
        return f"{float(value):.1f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "n/a"

    return (
        "reported", valid, rows, str(quality.get("status") or "limited"),
        oi_ok, pct("oi_coverage_pct"),
        "" if oi_ok else str(quality.get("reason") or "OI quality insufficient"),
        vol_ok, pct("volume_coverage_pct"),
        "" if vol_ok else str(quality.get("volume_reason") or "Volume quality insufficient"),
    )



_PROMPT_VOLATILE_KEYS = {
    "timestamp", "market_snapshot_timestamp", "observed_at",
    "fetched_at", "updated_at", "received_at", "last_updated",
    "created_at", "calculated_at", "refresh_time",
}


def _stable_prompt_value(value, key=""):
    """Normalize prompt-relevant values, excluding observation timestamps."""
    name = str(key).lower()

    if name in _PROMPT_VOLATILE_KEYS or name.endswith("_timestamp") or name.endswith("_at"):
        return None

    if isinstance(value, dict):
        return tuple(
            (str(k), _stable_prompt_value(v, str(k)))
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if str(k).lower() not in _PROMPT_VOLATILE_KEYS
            and not str(k).lower().endswith("_timestamp")
            and not str(k).lower().endswith("_at")
        )

    if isinstance(value, (list, tuple)):
        return tuple(_stable_prompt_value(v, name) for v in value[:20])

    if value is None or isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return str(value)
        if "pcr" in name:
            return round(number, 3)
        if any(part in name for part in ("pct", "percent")):
            return round(number, 2)
        if "strike" in name:
            return round(number, 0)
        if "oi" in name or "volume" in name:
            return int(round(number / 10000.0) * 10000)
        if any(part in name for part in (
            "price", "spot", "pain", "premium", "close"
        )):
            return round(number, 0)
        return round(number, 3)

    if isinstance(value, str):
        return value.strip()

    return str(value)


def _spot_price_bucket(value):
    """Ignore tiny spot noise but invalidate after a meaningful 5-point move."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(value)
    return math.floor(number / 5.0)


def _market_input_fingerprint(market_data: dict) -> tuple:
    """Fingerprint the material market facts behind an AI explanation."""
    spot = market_data.get("spot")
    if not isinstance(spot, dict):
        spot = {}

    decision = market_data.get("decision")
    if not isinstance(decision, dict):
        decision = {}

    decision_fields = (
        "signal_action", "action", "preferred_side", "bias", "direction",
        "signal_strength", "confidence", "hard_gated",
        "market_regime_no_trade", "signal_lifecycle",
        "market_bias", "bullish_probability", "bearish_probability",
        "bull_score", "bear_score", "forecast", "risk", "reasons",
    )
    decision_facts = {
        field: decision[field]
        for field in decision_fields
        if field in decision
    }

    supporting = {}
    for field in (
        "vix", "technicals", "technical_indicators", "indicators",
        "market_regime", "futures_premium", "futures_premium_pct",
    ):
        if field in market_data:
            supporting[field] = market_data[field]

    prompt_macd = market_data.get("macd")
    prompt_macd_value = (
        prompt_macd.get("macd", 0)
        if isinstance(prompt_macd, dict) else 0
    )

    facts = {
        "prompt_symbol": market_data.get("symbol", "NIFTY"),
        "prompt_rsi": _stable_prompt_value(
            market_data.get("rsi", 50), "rsi"
        ),
        "prompt_macd": _stable_prompt_value(
            prompt_macd_value, "macd"
        ),
        "prompt_support_resistance": _stable_prompt_value(
            market_data.get("support_resistance") or {},
            "support_resistance",
        ),
        "spot_price_bucket_5": _spot_price_bucket(spot.get("price")),
        "spot_change_percent": _stable_prompt_value(
            spot.get("change_percent"), "change_percent"
        ),
        "market_open": spot.get("market_open"),
        "pcr": _stable_prompt_value(market_data.get("pcr"), "pcr"),
        "max_pain": _stable_prompt_value(market_data.get("max_pain"), "max_pain"),
        "oi_summary": _stable_prompt_value(
            market_data.get("oi_summary") or {}, "oi_summary"
        ),
        "multi_timeframe": _stable_prompt_value(
            market_data.get("multi_timeframe") or {}, "multi_timeframe"
        ),
        "decision": _stable_prompt_value(decision_facts, "decision"),
        "supporting": _stable_prompt_value(supporting, "supporting"),
    }
    return tuple(sorted(facts.items()))


def _make_key(symbol: str, expiry: str, market_data: dict) -> str:
    # AI is an explanation/validation layer, not the live decision engine.
    # Do not key it on the live-calculation timestamp: that timestamp changes
    # on every market refresh and would defeat the shared AI TTL cache.
    ttl = max(1, int(settings.analysis_cache_ttl))
    bucket = math.floor(time.time() / ttl)
    return f"{symbol}:{expiry or ''}:ai_bucket:{bucket}:quality:{_quality_fingerprint(market_data)!r}:facts:{_market_input_fingerprint(market_data)!r}"


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
