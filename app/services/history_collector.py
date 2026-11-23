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


OPTION_CHAIN_SNAPSHOT_ROTATION_INTERVAL_SEC = 15.0
OPTION_CHAIN_SNAPSHOT_START_DELAY_SEC = 45.0
OPTION_CHAIN_SNAPSHOT_CLOSED_RECHECK_SEC = 60.0
OPTION_CHAIN_SNAPSHOT_FOREGROUND_GRACE_SEC = 2.0


async def _wait_for_option_chain_foreground_grace() -> None:
    """Give foreground requests a short head start over scheduled polling."""
    await asyncio.sleep(OPTION_CHAIN_SNAPSHOT_FOREGROUND_GRACE_SEC)


async def _collect_option_chain_snapshot_once(
    analyzer: MarketAnalyzer,
    symbol: str,
) -> bool:
    """Fetch one chain via the shared DataFetcher persistence path."""
    if not is_market_hours_ist():
        return False

    priority_token = set_angel_call_priority(ANGEL_PRIORITY_BACKGROUND)
    try:
        fetcher = getattr(analyzer, "fetcher", None)
        get_chain = getattr(fetcher, "get_option_chain", None)
        if not callable(get_chain):
            raise RuntimeError("shared option-chain fetcher unavailable")

        # Let foreground HTTP work become the single-flight owner when it
        # arrives at the same time. Background callers may join foreground
        # work; foreground callers must not wait for scheduler-owned I/O.
        await _wait_for_option_chain_foreground_grace()
        chain = await get_chain(symbol, expiry=None)
        rows = chain.get("data") if isinstance(chain, dict) else None
        if not rows:
            logger.warning(
                "Option-chain snapshot poll returned no rows for %s", symbol
            )
            return False
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Option-chain snapshot poll failed for %s (%s)",
            symbol,
            type(exc).__name__,
        )
        return False
    finally:
        reset_angel_call_priority(priority_token)


async def run_periodic_option_chain_snapshots(
    analyzer: MarketAnalyzer,
) -> None:
    """Poll one index at a time during market hours; never place trades."""
    symbols = settings.history_collector_symbols
    if not symbols:
        logger.warning("Option-chain snapshot scheduler disabled: no symbols")
        return

    logger.info(
        "Option-chain snapshot scheduler started: symbols=%s interval=%.1fs",
        symbols,
        OPTION_CHAIN_SNAPSHOT_ROTATION_INTERVAL_SEC,
    )

    # Leave startup warmup and initial application traffic undisturbed.
    await asyncio.sleep(OPTION_CHAIN_SNAPSHOT_START_DELAY_SEC)
    cycle_index = 0

    while True:
        try:
            if not is_market_hours_ist():
                await asyncio.sleep(OPTION_CHAIN_SNAPSHOT_CLOSED_RECHECK_SEC)
                continue

            symbol = symbols[cycle_index % len(symbols)]
            cycle_index += 1
            started = asyncio.get_running_loop().time()
            has_rows = await _collect_option_chain_snapshot_once(
                analyzer, symbol
            )
            elapsed = asyncio.get_running_loop().time() - started
            logger.info(
                "Option-chain snapshot poll complete: symbol=%s has_rows=%s elapsed=%.1fs",
                symbol,
                has_rows,
                elapsed,
            )

            # Pause after each poll even if a broker request is slow. This
            # avoids turning a slow request into a burst of immediate calls.
            await asyncio.sleep(OPTION_CHAIN_SNAPSHOT_ROTATION_INTERVAL_SEC)

        except asyncio.CancelledError:
            logger.info("Option-chain snapshot scheduler stopped")
            raise
        except Exception:
            logger.exception("Option-chain snapshot scheduler iteration failed")
            await asyncio.sleep(OPTION_CHAIN_SNAPSHOT_ROTATION_INTERVAL_SEC)


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
# FORWARD_PAPER_CAPTURE_SCHEDULER_PHASE2B
# Evaluate one configured index at a time, during market hours only.
# Uses the shared analyzer, existing strategy logic, background Angel priority,
# and a short foreground grace. No broker orders are sent by this task.
PAPER_CAPTURE_ROTATION_INTERVAL_SEC = 15.0
PAPER_CAPTURE_START_DELAY_SEC = 75.0
PAPER_CAPTURE_CLOSED_RECHECK_SEC = 60.0


async def _run_strategy_recommendation_for_capture(
    analyzer: MarketAnalyzer,
    ai: AIEngine,
    symbol: str,
) -> bool:
    # Import lazily to avoid router/service import cycles during app startup.
    from app.api.routes.strategy import strike_recommendation

    priority_token = set_angel_call_priority(ANGEL_PRIORITY_BACKGROUND)
    try:
        await _wait_for_option_chain_foreground_grace()
        result = await strike_recommendation(
            symbol=str(symbol).upper(),
            expiry=None,
            cache_bust="",
            analyzer=analyzer,
            ai=ai,
        )
        if isinstance(result, dict):
            logger.info(
                "Forward paper-capture evaluation complete: "
                "symbol=%s action=%s lifecycle=%s",
                str(symbol).upper(),
                result.get("best_strategy", "unknown"),
                result.get("signal_lifecycle", "unknown"),
            )
        else:
            logger.info(
                "Forward paper-capture evaluation completed for %s",
                str(symbol).upper(),
            )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "Forward paper-capture evaluation failed for %s (%s)",
            str(symbol).upper(),
            type(exc).__name__,
        )
        return False
    finally:
        reset_angel_call_priority(priority_token)


async def run_periodic_paper_capture(analyzer: MarketAnalyzer) -> None:
    """
    Background trigger for the existing strategy route and ledger capture.

    Rotates through configured symbols sequentially. Closed-market periods do
    not call the strategy route. This reuses the same analyzer and strategy
    implementation instead of duplicating decision or capture rules.
    """
    symbols = [
        str(symbol).upper()
        for symbol in settings.history_collector_symbols
        if str(symbol).strip()
    ]
    if not symbols:
        logger.warning("Forward paper-capture scheduler disabled: no symbols")
        return

    logger.info(
        "Forward paper-capture scheduler started: symbols=%s interval=%.1fs",
        symbols,
        PAPER_CAPTURE_ROTATION_INTERVAL_SEC,
    )
    await asyncio.sleep(PAPER_CAPTURE_START_DELAY_SEC)

    ai = None
    cycle_index = 0
    while True:
        if not is_market_hours_ist():
            await asyncio.sleep(PAPER_CAPTURE_CLOSED_RECHECK_SEC)
            continue

        symbol = symbols[cycle_index % len(symbols)]
        cycle_index += 1
        if ai is None:
            ai = AIEngine()

        started = asyncio.get_running_loop().time()
        try:
            ok = await _run_strategy_recommendation_for_capture(
                analyzer, ai, symbol
            )
        except asyncio.CancelledError:
            logger.info("Forward paper-capture scheduler stopped")
            raise
        except Exception:
            logger.exception(
                "Forward paper-capture scheduler iteration failed for %s",
                symbol,
            )
            ok = False

        elapsed = asyncio.get_running_loop().time() - started
        logger.info(
            "Forward paper-capture poll complete: symbol=%s success=%s elapsed=%.1fs",
            symbol,
            ok,
            elapsed,
        )
        await asyncio.sleep(
            max(0.0, PAPER_CAPTURE_ROTATION_INTERVAL_SEC - elapsed)
        )


# FORWARD_PAPER_OUTCOME_GRADING_PHASE1
# Grades persisted signal observations only; it never submits or journals trades.
# This loop also runs outside market hours so end-of-session horizons can mature.
PAPER_OUTCOME_GRADING_INTERVAL_SEC = 60.0
PAPER_OUTCOME_GRADING_START_DELAY_SEC = 90.0


async def run_periodic_forward_paper_outcomes() -> None:
    """Persist one outcome per eligible ledger entry and configured horizon."""
    logger.info(
        "Forward paper-outcome scheduler started: interval=%.1fs",
        PAPER_OUTCOME_GRADING_INTERVAL_SEC,
    )
    await asyncio.sleep(PAPER_OUTCOME_GRADING_START_DELAY_SEC)

    while True:
        started = asyncio.get_running_loop().time()
        try:
            # Lazy import keeps startup imports and service dependency edges small.
            from app.services.forward_paper_outcomes import (
                process_forward_paper_outcomes,
            )

            stats = await process_forward_paper_outcomes()
            logger.info(
                "Forward paper-outcome cycle complete: "
                "ledger_seen=%s eligible=%s created=%s updated=%s "
                "pending=%s matured=%s session_unavailable=%s",
                stats.get("ledger_rows_seen", 0),
                stats.get("eligible_entries", 0),
                stats.get("outcome_rows_created", 0),
                stats.get("outcome_rows_updated", 0),
                stats.get("pending_rows", 0),
                stats.get("matured_rows", 0),
                stats.get("session_unavailable_rows", 0),
            )
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(
                max(0.0, PAPER_OUTCOME_GRADING_INTERVAL_SEC - elapsed)
            )
        except asyncio.CancelledError:
            logger.info("Forward paper-outcome scheduler stopped")
            raise
        except Exception:
            logger.exception("Forward paper-outcome grading iteration failed")
            await asyncio.sleep(PAPER_OUTCOME_GRADING_INTERVAL_SEC)
