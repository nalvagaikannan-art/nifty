"""Persistent strategy signal history and restart-safe lifecycle state."""

from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional
import threading
import logging

from sqlalchemy import select, delete
from app.database import AsyncSessionLocal
from app.models import SignalState, SignalHistory

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_history: Dict[str, deque] = {}
MAX_HISTORY = 20


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_signal_dict(row: SignalHistory) -> dict:
    ts = row.timestamp or _now_utc()
    return {
        "symbol": row.symbol,
        "strategy": row.strategy,
        "score": row.score,
        "market_state": row.market_state,
        "confidence": row.confidence,
        "spot": row.spot,
        "pcr": row.pcr,
        "vix": row.vix,
        "reversal": bool(row.reversal),
        "reversal_type": row.reversal_type or "",
        "timestamp": ts.strftime("%H:%M:%S"),
        "date": ts.strftime("%d-%b"),
        "reasons": row.reasons or [],
    }


def record_signal(symbol: str, strategy: str, score: int, market_state: str,
                  confidence: int, spot: float, pcr: float, vix: float,
                  reasons: List[str] = None) -> dict:
    """Compatibility helper: records in the process cache immediately."""
    with _lock:
        hist = _history.setdefault(symbol, deque(maxlen=MAX_HISTORY))
        prev = hist[-1] if hist else None
        prev_strategy = prev.get("strategy") if prev else None
        reversal = bool(prev_strategy and prev_strategy not in (strategy, "WAIT")
                        and strategy not in ("WAIT", ""))
        reversal_type = f"{prev_strategy} → {strategy}" if prev_strategy and prev_strategy != strategy else ""
        now = _now_utc()
        signal = {
            "symbol": symbol, "strategy": strategy, "score": score,
            "market_state": market_state, "confidence": confidence,
            "spot": spot, "pcr": round(pcr, 3) if pcr else 0,
            "vix": round(vix, 1) if vix else 0, "reversal": reversal,
            "reversal_type": reversal_type, "timestamp": now.strftime("%H:%M:%S"),
            "date": now.strftime("%d-%b"), "reasons": reasons or [],
        }
        hist.append(signal)
        return signal


async def load_signal_state(symbol: str) -> Optional[dict]:
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(SignalState).where(SignalState.symbol == symbol))
            row = result.scalar_one_or_none()
            if not row:
                return None
            return {
                "symbol": row.symbol, "active_side": row.active_side,
                "candidate_side": row.candidate_side,
                "confirmations": row.confirmations,
                "reversal_confirmations": row.reversal_confirmations,
                "lifecycle": row.lifecycle,
                "last_confirmation_at": row.last_confirmation_at,
                "last_evaluation_at": row.last_evaluation_at,
                "strategy": row.strategy, "strategy_score": row.strategy_score,
                "margin": row.margin,
            }
    except Exception as exc:
        logger.warning("Persistent signal state load failed for %s: %s", symbol, exc)
        return None


async def save_signal_state(state: dict) -> None:
    symbol = str(state["symbol"]).upper()
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(SignalState).where(SignalState.symbol == symbol))
            row = result.scalar_one_or_none()
            if row is None:
                row = SignalState(symbol=symbol)
                db.add(row)
            row.active_side = state.get("active_side", "NONE")
            row.candidate_side = state.get("candidate_side", "NONE")
            row.confirmations = int(state.get("confirmations", 0) or 0)
            row.reversal_confirmations = int(state.get("reversal_confirmations", 0) or 0)
            row.lifecycle = state.get("lifecycle", "WAIT")
            row.last_confirmation_at = state.get("last_confirmation_at")
            row.last_evaluation_at = state.get("last_evaluation_at")
            row.strategy = state.get("strategy", "WAIT")
            row.strategy_score = float(state.get("strategy_score", 0) or 0)
            row.margin = float(state.get("margin", 0) or 0)
            await db.commit()
    except Exception as exc:
        logger.warning("Persistent signal state save failed for %s: %s", symbol, exc)


async def record_signal_persistent(symbol: str, strategy: str, score: int,
                                   market_state: str, confidence: int, spot: float,
                                   pcr: float, vix: float, reasons: List[str] = None) -> dict:
    symbol = str(symbol).upper()
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SignalHistory).where(SignalHistory.symbol == symbol)
                .order_by(SignalHistory.id.desc()).limit(1)
            )
            prev = result.scalar_one_or_none()
            prev_strategy = prev.strategy if prev else None
            reversal = bool(prev_strategy and prev_strategy not in (strategy, "WAIT")
                            and strategy not in ("WAIT", ""))
            reversal_type = f"{prev_strategy} → {strategy}" if prev_strategy and prev_strategy != strategy else ""
            row = SignalHistory(
                symbol=symbol, strategy=strategy, score=float(score or 0),
                market_state=market_state or "UNKNOWN", confidence=float(confidence or 0),
                spot=float(spot or 0), pcr=float(pcr or 0), vix=float(vix or 0),
                reversal=int(reversal), reversal_type=reversal_type, reasons=reasons or [],
            )
            db.add(row)
            await db.flush()
            ids = await db.execute(
                select(SignalHistory.id).where(SignalHistory.symbol == symbol)
                .order_by(SignalHistory.id.desc()).offset(MAX_HISTORY)
            )
            old_ids = [x[0] for x in ids.all()]
            if old_ids:
                await db.execute(delete(SignalHistory).where(SignalHistory.id.in_(old_ids)))
            await db.commit()
            return _to_signal_dict(row)
    except Exception as exc:
        logger.warning("Persistent signal history write failed for %s: %s", symbol, exc)
        return record_signal(symbol, strategy, score, market_state, confidence, spot, pcr, vix, reasons)


async def get_history_persistent(symbol: str) -> List[dict]:
    symbol = str(symbol).upper()
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SignalHistory).where(SignalHistory.symbol == symbol)
                .order_by(SignalHistory.id.desc()).limit(MAX_HISTORY)
            )
            return [_to_signal_dict(row) for row in result.scalars().all()]
    except Exception as exc:
        logger.warning("Persistent signal history read failed for %s: %s", symbol, exc)
        return get_history(symbol)


def get_history(symbol: str) -> List[dict]:
    with _lock:
        return list(reversed(list(_history.get(symbol.upper(), deque()))))


def get_reversals(symbol: str) -> List[dict]:
    return [s for s in get_history(symbol) if s.get("reversal")]


def clear_history(symbol: str = None) -> None:
    with _lock:
        if symbol:
            _history.pop(symbol.upper(), None)
        else:
            _history.clear()
