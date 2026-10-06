"""Persistent strategy signal history and restart-safe lifecycle state."""

from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional
import threading
import logging
import json

from sqlalchemy import select, delete
from app.database import AsyncSessionLocal
from app.models import SignalState, SignalHistory, DailySignalLedger
from app.utils.helpers import now_utc_naive

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_history: Dict[str, deque] = {}
MAX_HISTORY = 20


def _now_utc() -> datetime:
    return now_utc_naive()


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
            else:
                # ATOMIC_LIFECYCLE_FIX_20260923
                # Do not allow a genuinely older in-flight request to
                # overwrite a newer lifecycle evaluation.
                incoming_eval = state.get("last_evaluation_at")
                stored_eval = row.last_evaluation_at

                if incoming_eval is not None and stored_eval is not None:
                    try:
                        inc = incoming_eval
                        st = stored_eval

                        if getattr(inc, "tzinfo", None) is not None:
                            inc = inc.astimezone(timezone.utc).replace(tzinfo=None)

                        if getattr(st, "tzinfo", None) is not None:
                            st = st.astimezone(timezone.utc).replace(tzinfo=None)

                        if inc < st:
                            logger.warning(
                                "Ignoring stale signal state for %s: "
                                "incoming_eval=%s < stored_eval=%s",
                                symbol, incoming_eval, stored_eval
                            )
                            await db.rollback()
                            return
                    except Exception as exc:
                        logger.warning(
                            "Signal-state timestamp comparison failed for %s: %s",
                            symbol, exc
                        )

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



ACTIONABLE_LEDGER_ACTIONS = {"BUY CE", "BUY PE", "SELL CE", "SELL PE"}


def _ledger_json_safe(value):
    """Return a JSON-serializable copy without changing source data."""
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return {"value": str(value)}


def _ledger_timestamp(value):
    """Parse an incoming ISO timestamp into a naive UTC datetime."""
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    return dt


async def record_daily_signal_ledger(
    *,
    symbol: str,
    action: str,
    option_type: str,
    strike: float,
    expiry: str = "",
    spot: float = 0.0,
    option_ltp_snapshot: float = None,
    entry_price: float = None,
    signal_strength: float = 0.0,
    confidence: float = 0.0,
    lifecycle: str = "UNKNOWN",
    confirmations: int = 0,
    market_snapshot_timestamp=None,
    technical_data_source: str = None,
    confluence=None,
    mtf_freshness=None,
    entry_snapshot=None,
    lifecycle_confirmation_at=None,
):
    """
    Permanently capture one actionable signal episode.

    Duplicate polling of the same lifecycle confirmation/market snapshot
    must not create another ledger row. WAIT is intentionally excluded.
    Outcome remains NULL for the evidence-collection phase.
    """
    symbol = str(symbol or "").upper()
    action = str(action or "").upper().strip()
    option_type = str(option_type or "").upper().strip()

    if action not in ACTIONABLE_LEDGER_ACTIONS:
        return None

    # Prefer the lifecycle confirmation timestamp as the episode anchor.
    # When unavailable, use the market snapshot timestamp so repeated
    # requests against the exact same snapshot deduplicate.
    anchor_dt = (
        _ledger_timestamp(lifecycle_confirmation_at)
        or _ledger_timestamp(market_snapshot_timestamp)
        or _now_utc()
    )

    event_dt = (
        _ledger_timestamp(market_snapshot_timestamp)
        or anchor_dt
    )

    anchor_iso = anchor_dt.isoformat()
    session_date = event_dt.date().isoformat()

    ledger_key = "|".join([
        symbol,
        action,
        option_type,
        str(float(strike or 0)),
        str(expiry or ""),
        session_date,
        anchor_iso,
    ])

    try:
        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(DailySignalLedger)
                .where(DailySignalLedger.ledger_key == ledger_key)
                .limit(1)
            )
            row = existing.scalar_one_or_none()

            if row is not None:
                return {
                    "id": row.id,
                    "ledger_key": row.ledger_key,
                    "duplicate": True,
                }

            row = DailySignalLedger(
                timestamp=event_dt,
                symbol=symbol,
                action=action,
                option_type=option_type,
                strike=float(strike or 0),
                expiry=str(expiry or ""),
                spot=float(spot or 0),
                option_ltp_snapshot=(
                    float(option_ltp_snapshot)
                    if option_ltp_snapshot is not None else None
                ),
                entry_price=(
                    float(entry_price)
                    if entry_price is not None else None
                ),
                signal_strength=float(signal_strength or 0),
                confidence=float(confidence or 0),
                lifecycle=str(lifecycle or "UNKNOWN"),
                confirmations=int(confirmations or 0),
                market_snapshot_timestamp=(
                    str(market_snapshot_timestamp)
                    if market_snapshot_timestamp is not None else None
                ),
                technical_data_source=(
                    str(technical_data_source)
                    if technical_data_source is not None else None
                ),
                confluence=_ledger_json_safe(confluence or {}),
                mtf_freshness=_ledger_json_safe(mtf_freshness or {}),
                entry_snapshot=_ledger_json_safe(entry_snapshot or {}),
                outcome=None,
                ledger_key=ledger_key,
            )

            db.add(row)
            await db.commit()
            await db.refresh(row)

            logger.info(
                "DAILY_SIGNAL_LEDGER_CAPTURED symbol=%s action=%s "
                "option_type=%s strike=%s lifecycle=%s key=%s",
                symbol,
                action,
                option_type,
                strike,
                lifecycle,
                ledger_key,
            )

            return {
                "id": row.id,
                "ledger_key": row.ledger_key,
                "duplicate": False,
            }

    except Exception as exc:
        logger.warning(
            "Daily signal ledger write failed for %s %s: %s",
            symbol,
            action,
            exc,
        )
        return None


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
