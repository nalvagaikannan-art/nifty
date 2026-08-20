"""
Accuracy API — Spec §29 (Prediction vs Actual) + §30 (Adaptive Weight hints)

Two complementary views, both computed on-demand from data this app has
already saved (AnalysisResult + MarketData) — no new external data source
needed:

  GET /api/accuracy/indicators/{symbol}  — per-indicator hit rate
      (accuracy_engine.py — was already fully written but never wired to
      any route; wiring it in here, unchanged).

  GET /api/accuracy/signals/{symbol}     — per-SIGNAL/action accuracy:
      overall, CALL BUY, PUT BUY, by market regime, by confidence range,
      at 5/10/15/30/60-minute horizons (signal_accuracy.py, new).
"""
import asyncio
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func
from app.database import AsyncSessionLocal
from app.models import MarketData, AnalysisResult, OptionData

from app.services.accuracy_engine import compute_indicator_accuracy
from app.services.signal_accuracy import (
    compute_signal_accuracy, compute_premium_accuracy, calibrate_confidence, HORIZONS_MINUTES,
)
from app.services.error_log import record_error, recent_errors
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

VALID_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}

# Each accuracy computation scans up to 60 days of MarketData/AnalysisResult
# rows in-process. Left unbounded, a slow/heavy computation on Render's free
# tier (shared CPU, 512MB) can run long enough that Render's own proxy kills
# the connection with an HTML 502/504 — which the browser can't parse as
# JSON, so it shows a blank "server error" with no useful detail. Failing
# fast here instead, with a real JSON error, is strictly better: the user
# gets an honest "try fewer days" message instead of a dead connection.
COMPUTE_TIMEOUT_SECONDS = 20


def _check_symbol(symbol: str) -> str:
    sym = symbol.upper()
    if sym not in VALID_SYMBOLS:
        raise HTTPException(400, detail=f"Unsupported symbol: {symbol}")
    return sym


async def _run_with_timeout(coro, *, endpoint: str, sym: str, timeout_detail: str, error_detail: str):
    try:
        return await asyncio.wait_for(coro, timeout=COMPUTE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(f"{endpoint} accuracy timed out for {sym} after {COMPUTE_TIMEOUT_SECONDS}s")
        record_error(endpoint, sym, timeout_detail)
        raise HTTPException(504, detail=timeout_detail)
    except Exception as e:
        logger.error(f"{endpoint} accuracy failed for {sym}: {e}")
        record_error(endpoint, sym, f"{error_detail}: {e}")
        raise HTTPException(500, detail=error_detail)


@router.get("/indicators/{symbol}")
async def indicator_accuracy(symbol: str, days: int = Query(15, ge=1, le=60)):
    sym = _check_symbol(symbol)
    return await _run_with_timeout(
        compute_indicator_accuracy(sym, days=days),
        endpoint="indicators", sym=sym,
        timeout_detail=f"Indicator accuracy for {days} days is taking too long — try a smaller range (7 days).",
        error_detail="Accuracy computation failed",
    )


@router.get("/signals/{symbol}")
async def signal_accuracy(
    symbol: str,
    days: int = Query(15, ge=1, le=60),
    horizon_minutes: int = Query(60, description=f"one of {HORIZONS_MINUTES}"),
):
    sym = _check_symbol(symbol)
    return await _run_with_timeout(
        compute_signal_accuracy(sym, days=days, horizon_minutes=horizon_minutes),
        endpoint="signals", sym=sym,
        timeout_detail=f"Signal accuracy for {days} days is taking too long — try a smaller range (7 days).",
        error_detail="Accuracy computation failed",
    )


@router.get("/premium/{symbol}")
async def premium_accuracy(
    symbol: str,
    days: int = Query(15, ge=1, le=60),
    horizon_minutes: int = Query(60, description=f"one of {HORIZONS_MINUTES}"),
):
    """Review #1: option-PREMIUM-based accuracy (would buying the actually-
    recommended contract have made money), as a distinct metric from
    /signals' spot-direction accuracy — see signal_accuracy.compute_premium_accuracy."""
    sym = _check_symbol(symbol)
    return await _run_with_timeout(
        compute_premium_accuracy(sym, days=days, horizon_minutes=horizon_minutes),
        endpoint="premium", sym=sym,
        timeout_detail=f"Premium accuracy for {days} days is taking too long — try a smaller range (7 days).",
        error_detail="Accuracy computation failed",
    )


@router.get("/calibration/{symbol}")
async def confidence_calibration(
    symbol: str,
    confidence: float = Query(..., ge=0, le=100, description="Current Signal Strength (0-100) to look up"),
    days: int = Query(30, ge=1, le=60),
    horizon_minutes: int = Query(60, description=f"one of {HORIZONS_MINUTES}"),
):
    """Review #4/#46: 'Signal Strength 85% ≠ 85% win probability.' Returns
    the historical win-rate for the confidence bucket `confidence` falls
    into, plus the full calibration curve (every bucket), computed from
    past graded signals — see signal_accuracy.calibrate_confidence.
    (Note: /api/analysis/ai/{symbol} already attaches this automatically
    under `confidence_calibration` — this endpoint is for looking it up
    standalone, e.g. for a different confidence value than the live one.)"""
    sym = _check_symbol(symbol)
    return await _run_with_timeout(
        calibrate_confidence(sym, confidence, days=days, horizon_minutes=horizon_minutes),
        endpoint="calibration", sym=sym,
        timeout_detail=f"Calibration for {days} days is taking too long — try a smaller range.",
        error_detail="Calibration computation failed",
    )


@router.get("/status/{symbol}")
async def accuracy_status(symbol: str):
    """Persistence diagnostics: proves whether history is actually being stored."""
    sym = _check_symbol(symbol)
    try:
        async with AsyncSessionLocal() as session:
            market_count = await session.scalar(
                select(func.count()).select_from(MarketData).where(MarketData.symbol == sym)
            )
            analysis_count = await session.scalar(
                select(func.count()).select_from(AnalysisResult)
                .where(AnalysisResult.symbol == sym)
                .where(AnalysisResult.analysis_type == "ai")
            )
            option_count = await session.scalar(
                select(func.count()).select_from(OptionData).where(OptionData.symbol == sym)
            )
            last_market = await session.scalar(
                select(func.max(MarketData.timestamp)).where(MarketData.symbol == sym)
            )
            last_analysis = await session.scalar(
                select(func.max(AnalysisResult.timestamp))
                .where(AnalysisResult.symbol == sym)
                .where(AnalysisResult.analysis_type == "ai")
            )
            return {
                "symbol": sym,
                "saved": {
                    "market_snapshots": int(market_count or 0),
                    "ai_signals": int(analysis_count or 0),
                    "option_rows": int(option_count or 0),
                },
                "last_saved_utc": {
                    "market": last_market.isoformat() if last_market else None,
                    "analysis": last_analysis.isoformat() if last_analysis else None,
                },
                "database_configured": bool(__import__("app.config", fromlist=["settings"]).settings.database_url),
                "note": "If DATABASE_URL is empty on Render, SQLite is local/ephemeral and history can disappear after restart/redeploy.",
                # Review (Tamil bug report): "ஒவ்வொரு call-உம் save பண்ணி
                # பேஜ்ல warning கொடுக்கணும்" — every accuracy-route failure
                # (from any of the 4 endpoints this page calls) is recorded
                # by _run_with_timeout()/record_error() above, and surfaced
                # here so the page itself shows *why* things failed instead
                # of a dead "server error".
                "recent_errors": recent_errors(sym, limit=5),
            }
    except Exception as e:
        logger.exception("Persistence status failed for %s", sym)
        record_error("status", sym, f"Persistence status failed: {e}")
        raise HTTPException(500, detail="Persistence status failed")
