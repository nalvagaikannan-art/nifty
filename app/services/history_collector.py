from app.services.history_service import save_analysis_result
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
import time
import logging

from app.config import settings
from app.services.market_analyzer import MarketAnalyzer
from app.services.angel_one import (
    set_angel_call_priority,
    reset_angel_call_priority,
    ANGEL_PRIORITY_BACKGROUND,
)
from app.services.ai_engine import AIEngine
from app.services.history_service import save_market_snapshot
from app.exceptions import AIProviderError, MarketDataError
from app.utils.helpers import is_market_hours_ist

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
    # During regular NSE market hours, foreground HTTP analysis owns the
    # Angel One broker capacity. Do not start a second broker-heavy
    # get_full_market_overview() from the history collector.
    #
    # The collector remains active outside market hours so historical
    # persistence is still available without competing with live analysis.
    if is_market_hours_ist():
        logger.info(
            "History collector: skipping %s during market hours "
            "(foreground live analysis owns Angel capacity)",
            symbol,
        )
        return

    # Imported lazily to avoid a circular import (analysis.py imports this
    # module's sibling history_service, and this module needs analysis.py's
    # shared result-builder — importing it at module load time would create
    # a cycle since analysis.py's router is imported by main.py before this
    # module is).
    from app.api.routes.analysis import build_ai_analysis

    priority_token = set_angel_call_priority(
        ANGEL_PRIORITY_BACKGROUND
    )
    try:
        # Use the same explicit keyword form as HTTP routes so the canonical
        # cache key is identical even on older cache implementations.
        market_data = await analyzer.get_full_market_overview(symbol, expiry=None)
        spot = market_data.get("spot") or {}

        try:
            ai = AIEngine()
        except AIProviderError:
            # No AI key configured at all — market snapshot above still saved,
            # just skip the AI-signal half. Not an error worth logging every cycle.
            return

        try:
            result = await build_ai_analysis(symbol, analyzer, ai)
            # History collector is the single owner of historical AI persistence.
            await save_analysis_result(symbol, "ai", result)
        except Exception:
            logger.exception(
                "History collector: AI analysis save failed for %s", symbol
            )

    except MarketDataError as e:
        logger.warning(
            "History collector: market data unavailable for %s: %s",
            symbol,
            e,
        )
    except Exception:
        logger.exception(
            "History collector: unexpected error fetching market data for %s",
            symbol,
        )
    finally:
        # ALWAYS restore the caller's priority, including every return/error path.
        reset_angel_call_priority(priority_token)

async def _collect_once(analyzer: MarketAnalyzer, symbol: str) -> None:
    lock = await _get_collection_lock(symbol)
    if lock.locked():
        logger.info("History collector: skipping overlapping run for %s", symbol)
        return
    async with lock:
        await _collect_once_unlocked(analyzer, symbol)



LIVE_MARKET_SNAPSHOT_INTERVAL_SEC = 60
LIVE_MARKET_SNAPSHOT_MAX_AGE_SEC = 10


async def run_periodic_live_market_snapshots() -> None:
    """
    Persist fresh Angel WebSocket index LTPs independently of browser/API traffic.

    This task must remain broker-call-free:
      - read the already-connected in-memory Angel WebSocket ticks
      - reject stale/missing exchange timestamps
      - persist at most once per symbol per exchange tick
      - never manufacture a market snapshot when the WebSocket is stale
    """
    from app.services.angel_live_feed import angel_live_feed

    last_persisted_exchange_ts = {}

    logger.info(
        "Live MarketData WS writer started: interval=%ss max_age=%ss",
        LIVE_MARKET_SNAPSHOT_INTERVAL_SEC,
        LIVE_MARKET_SNAPSHOT_MAX_AGE_SEC,
    )

    while True:
        try:
            if not is_market_hours_ist():
                await asyncio.sleep(15)
                continue

            snapshot = angel_live_feed.get_snapshot()
            now = time.time()

            for symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
                tick = snapshot.get(symbol)
                if not isinstance(tick, dict):
                    continue

                try:
                    price = float(tick.get("ltp"))
                    exchange_ts = float(tick.get("exchange_timestamp")) / 1000.0
                except (TypeError, ValueError):
                    continue

                if price <= 0 or exchange_ts <= 0:
                    continue

                age_sec = now - exchange_ts

                # Never turn an old WS tick into a new historical snapshot.
                if age_sec < -5 or age_sec > LIVE_MARKET_SNAPSHOT_MAX_AGE_SEC:
                    continue

                # Avoid writing the same exchange observation repeatedly.
                if last_persisted_exchange_ts.get(symbol) == exchange_ts:
                    continue

                await save_market_snapshot(
                    {
                        "symbol": symbol,
                        "price": price,
                        "timestamp": tick.get("timestamp"),
                        "exchange_timestamp": tick.get("exchange_timestamp"),
                        "market_open": True,
                        "data_source": "angel_one_websocket",
                    }
                )

                last_persisted_exchange_ts[symbol] = exchange_ts

                logger.info(
                    "WS MarketData snapshot persisted: symbol=%s price=%.2f "
                    "exchange_age=%.1fs",
                    symbol,
                    price,
                    age_sec,
                )

        except asyncio.CancelledError:
            logger.info("Live MarketData WS writer stopped")
            raise
        except Exception as exc:
            logger.exception(
                "Live MarketData WS writer iteration failed: %s",
                exc,
            )

        await asyncio.sleep(LIVE_MARKET_SNAPSHOT_INTERVAL_SEC)


async def run_periodic_collection(analyzer: MarketAnalyzer) -> None:
    interval_minutes = settings.history_collector_interval_minutes
    if interval_minutes <= 0:
        logger.info(
            "History collector disabled "
            "(HISTORY_COLLECTOR_INTERVAL_MINUTES=0)"
        )
        return

    symbols = settings.history_collector_symbols
    interval_seconds = interval_minutes * 60

    if not symbols:
        logger.warning(
            "History collector disabled: no symbols configured"
        )
        return

    logger.info(
        "History collector started: symbols=%s every %s minute(s), "
        "rotating one symbol per cycle",
        symbols,
        interval_minutes,
    )

    # Let application startup, Angel login, instrument-master warmup and the
    # first browser request settle before the first background collection.
    await asyncio.sleep(30)

    cycle_index = 0

    while True:
        cycle_started = asyncio.get_running_loop().time()

        # Only ONE broker-heavy full overview per cycle.
        # Example with 3 symbols:
        # cycle 1 -> NIFTY
        # cycle 2 -> BANKNIFTY
        # cycle 3 -> FINNIFTY
        # then repeat.
        symbol = symbols[cycle_index % len(symbols)]
        cycle_index += 1

        try:
            await _collect_once(analyzer, symbol)
        except Exception:
            # One failed symbol must never kill the collector loop.
            logger.exception(
                "History collector: unhandled error for %s",
                symbol,
            )

        elapsed = asyncio.get_running_loop().time() - cycle_started
        remaining = max(0.0, interval_seconds - elapsed)

        logger.info(
            "History collector cycle complete: symbol=%s "
            "elapsed=%.1fs next_cycle_in=%.1fs",
            symbol,
            elapsed,
            remaining,
        )

        await asyncio.sleep(remaining)
