"""
Background history collector.

ROOT CAUSE this fixes: MarketData/AnalysisResult rows were only ever written
as a *side-effect of a page render* — save_market_snapshot() ran inside
/api/dashboard/summary, save_analysis_result() ran inside
/api/analysis/ai/{symbol}. If nobody had the Dashboard or Analysis page open
in a browser tab, NOTHING got saved, no matter how long the server had been
running. That's exactly why the Accuracy page (and the OI-change-over-time
feature in history_service.get_oi_change_since) kept showing "insufficient
data" / "data கிடைக்கவில்லை" — the history tables were simply empty.

This module runs an asyncio background task, started once in app/main.py's
lifespan, that calls the same save path on a fixed interval for every
configured symbol — independent of any request. It reuses
app.state.market_analyzer (the same shared instance every route uses, so it
benefits from the same short-TTL cache and doesn't create extra DataFetcher
sessions) and a fresh AIEngine() (cheap to construct, same as
app/api/deps.get_ai_engine).

Failures for one symbol (market closed, AI provider down, NSE blocked, etc.)
are logged and skipped — they never crash the loop or block other symbols.
"""
import asyncio
import logging

from app.config import settings
from app.services.market_analyzer import MarketAnalyzer
from app.services.ai_engine import AIEngine
from app.services.history_service import save_market_snapshot
from app.exceptions import AIProviderError, MarketDataError

logger = logging.getLogger(__name__)



# Prevent duplicate history collection for the same symbol if more than one
# collector task is accidentally started.
_collection_locks = {}

async def _get_collection_lock(symbol: str) -> asyncio.Lock:
    lock = _collection_locks.get(symbol)
    if lock is None:
        lock = asyncio.Lock()
        _collection_locks[symbol] = lock
    return lock

async def _collect_once_unlocked(analyzer: MarketAnalyzer, symbol: str) -> None:
    # Imported lazily to avoid a circular import (analysis.py imports this
    # module's sibling history_service, and this module needs analysis.py's
    # shared result-builder — importing it at module load time would create
    # a cycle since analysis.py's router is imported by main.py before this
    # module is).
    from app.api.routes.analysis import build_ai_analysis

    try:
        # Use the same explicit keyword form as HTTP routes so the canonical
        # cache key is identical even on older cache implementations.
        market_data = await analyzer.get_full_market_overview(symbol, expiry=None)
    except MarketDataError as e:
        logger.warning("History collector: market data unavailable for %s: %s", symbol, e)
        return
    except Exception:
        logger.exception("History collector: unexpected error fetching market data for %s", symbol)
        return

    spot = market_data.get("spot") or {}
    if spot.get("price") is not None:
        await save_market_snapshot(spot)

    try:
        ai = AIEngine()
    except AIProviderError:
        # No AI key configured at all — market snapshot above still saved,
        # just skip the AI-signal half. Not an error worth logging every cycle.
        return

    try:
        await build_ai_analysis(symbol, analyzer, ai)  # saves AnalysisResult internally
    except Exception:
        logger.exception("History collector: AI analysis save failed for %s", symbol)


async def _collect_once(analyzer: MarketAnalyzer, symbol: str) -> None:
    lock = await _get_collection_lock(symbol)
    if lock.locked():
        logger.info("History collector: skipping overlapping run for %s", symbol)
        return
    async with lock:
        await _collect_once_unlocked(analyzer, symbol)


async def run_periodic_collection(analyzer: MarketAnalyzer) -> None:
    interval_minutes = settings.history_collector_interval_minutes
    if interval_minutes <= 0:
        logger.info("History collector disabled (HISTORY_COLLECTOR_INTERVAL_MINUTES=0)")
        return

    symbols = settings.history_collector_symbols
    interval_seconds = interval_minutes * 60
    logger.info(
        "History collector started: symbols=%s every %s minute(s)",
        symbols, interval_minutes,
    )

    # Do not compete with Render startup, Angel login, instrument-master
    # warmup and the first browser load. A cold collector run can otherwise
    # start the same expensive market-data pipeline at exactly the same time
    # as the first dashboard request.
    await asyncio.sleep(30)

    while True:
        for symbol in symbols:
            try:
                await _collect_once(analyzer, symbol)
            except Exception:
                # Belt-and-braces — _collect_once already catches its own
                # errors, but one symbol's bug must never kill the loop for
                # the rest, or stop future cycles.
                logger.exception("History collector: unhandled error for %s", symbol)
        await asyncio.sleep(interval_seconds)
