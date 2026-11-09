"""Read-only historical call tracker using DailySignalLedger and quote history.

This module never places orders, creates paper trades, or writes to the database.
Level touches are sampled-quote observations, not proof of an executed trade.
"""
import math
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import DailySignalLedger, OptionQuoteHistory
from app.utils.expiry import canonical_expiry, expiry_matches


VALID_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}
VALID_ACTIONS = {"BUY CE", "BUY PE", "SELL CE", "SELL PE"}
IST = ZoneInfo("Asia/Kolkata")
MAX_LEDGER_ROWS = 1000
MAX_QUOTE_ROWS_PER_CONTRACT = 20000


def _positive(value):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _utc_naive(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        # Database timestamps in this application's persisted history are UTC.
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_utc(value):
    value = _utc_naive(value)
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _snapshot(row):
    value = getattr(row, "entry_snapshot", None)
    return value if isinstance(value, dict) else {}


def _decision(row):
    value = _snapshot(row).get("strategy_decision")
    return value if isinstance(value, dict) else {}


def _entry_classification(row):
    """Classify frozen entry authority without promoting legacy V1 rows."""
    from app.services.entry_risk_authority import is_entry_risk_approved

    decision = _decision(row)
    metadata_version = decision.get("metadata_version")
    if type(metadata_version) is not int or metadata_version not in (1, 2):
        return "ENTRY_AUTHORITY_UNKNOWN"

    allowed = decision.get("fresh_entry_allowed")
    if type(allowed) is not bool:
        return "ENTRY_AUTHORITY_UNKNOWN"

    action = str(getattr(row, "action", "") or "").upper().strip()
    decision_lifecycle = str(
        decision.get("lifecycle") or ""
    ).strip().upper()
    row_lifecycle = str(
        getattr(row, "lifecycle", "") or ""
    ).strip().upper()
    signal_action = str(decision.get("signal_action") or "").strip().upper()

    is_hold = (
        decision_lifecycle.startswith("HOLD_")
        or row_lifecycle.startswith("HOLD_")
        or signal_action.startswith("HOLD ")
    )

    if allowed is True:
        expected_lifecycle = (
            "CONFIRMED_CALL" if action in ("BUY CE", "SELL PE")
            else "CONFIRMED_PUT"
        )
        if (
            action not in VALID_ACTIONS
            or decision_lifecycle != expected_lifecycle
            or row_lifecycle != expected_lifecycle
            or signal_action != action
        ):
            return "ENTRY_AUTHORITY_INCONSISTENT"

        if metadata_version == 1:
            return "LEGACY_RISK_UNVERIFIED"

        authority = decision.get("entry_risk_authority")
        if (
            not is_entry_risk_approved(authority)
            or authority.get("action") != action
        ):
            return "ENTRY_AUTHORITY_INCONSISTENT"

        return "FRESH_ENTRY_ALLOWED"

    if is_hold:
        return "HOLD_EXISTING_DIRECTION"

    return "ENTRY_DENIED"



def _validated_levels(row):
    """Return the saved option-premium levels only when their ordering is valid."""
    snapshot = _snapshot(row)
    action = str(getattr(row, "action", "") or "").upper().strip()
    entry = _positive(getattr(row, "entry_price", None))
    if entry is None:
        entry = _positive(snapshot.get("entry_price"))

    values = {
        "entry": entry,
        "SL": _positive(snapshot.get("sl")),
        "T1": _positive(snapshot.get("t1")),
        "T2": _positive(snapshot.get("t2")),
        "T3": _positive(snapshot.get("t3")),
    }
    if action not in VALID_ACTIONS:
        return None, "Unsupported action"
    if any(values[key] is None for key in ("entry", "SL", "T1", "T2", "T3")):
        return None, "Saved entry or one or more option-premium levels are missing"

    entry = values["entry"]
    sl, t1, t2, t3 = values["SL"], values["T1"], values["T2"], values["T3"]

    if action.startswith("BUY"):
        valid = sl < entry < t1 < t2 < t3
    else:
        valid = t3 < t2 < t1 < entry < sl

    if not valid:
        return None, "Saved SL/target ordering is invalid for this action"
    return values, None


def _saved_level_display(row):
    """Expose the saved contract levels even for HOLD/denied history rows.

    Displaying saved levels never changes the entry classification or grades
    a HOLD/denied row as a new call.
    """
    snapshot = _snapshot(row)
    entry = (
        _positive(getattr(row, "entry_price", None))
        or _positive(snapshot.get("entry_price"))
        or _positive(getattr(row, "option_ltp_snapshot", None))
    )
    return {
        "entry": entry,
        "SL": _positive(snapshot.get("sl")),
        "T1": _positive(snapshot.get("t1")),
        "T2": _positive(snapshot.get("t2")),
        "T3": _positive(snapshot.get("t3")),
    }


def _session_close_utc(event_ts):
    local_event = event_ts.replace(tzinfo=timezone.utc).astimezone(IST)
    local_close = datetime.combine(
        local_event.date(), dt_time(hour=15, minute=30), tzinfo=IST
    )
    return local_close.astimezone(timezone.utc).replace(tzinfo=None)


def _target_hit(action, price, target):
    if action.startswith("BUY"):
        return price >= target
    return price <= target


def _stop_hit(action, price, stop):
    if action.startswith("BUY"):
        return price <= stop
    return price >= stop


def _evaluate_quote_path(action, levels, quotes, now_utc, close_utc):
    """Grade the observed path; keep post-stop target crosses distinct."""
    clean = []
    for item in quotes:
        try:
            ts = _utc_naive(item[0])
            price = _positive(item[1])
        except (TypeError, IndexError, KeyError):
            continue
        if ts is not None and price is not None:
            clean.append((ts, price))
    clean.sort(key=lambda item: item[0])

    if not clean:
        return {
            "status": "NO_MATCHING_SOURCE_QUOTES",
            "first_barrier": None,
            "quote_count": 0,
            "targets_touched_anytime": [],
            "targets_before_stop": [],
            "targets_after_stop": [],
            "target_times": {"T1": None, "T2": None, "T3": None},
            "stop_loss_at": None,
            "last_quote_at": None,
            "last_quote_price": None,
        }

    target_times = {"T1": None, "T2": None, "T3": None}
    stop_at = None

    for ts, price in clean:
        for target_name in ("T1", "T2", "T3"):
            if target_times[target_name] is None and _target_hit(
                action, price, levels[target_name]
            ):
                target_times[target_name] = ts

        if stop_at is None and _stop_hit(action, price, levels["SL"]):
            stop_at = ts

    touched = [
        name for name in ("T1", "T2", "T3")
        if target_times[name] is not None
    ]
    before_stop = [
        name for name in ("T1", "T2", "T3")
        if target_times[name] is not None
        and (stop_at is None or target_times[name] < stop_at)
    ]
    after_stop = [
        name for name in ("T1", "T2", "T3")
        if stop_at is not None
        and target_times[name] is not None
        and target_times[name] > stop_at
    ]

    t1_at = target_times["T1"]
    if stop_at is not None and t1_at is not None and stop_at == t1_at:
        first_barrier = "AMBIGUOUS_SAME_TIMESTAMP"
        status = "AMBIGUOUS_STOP_TARGET_SAME_TIMESTAMP"
    elif stop_at is not None and (t1_at is None or stop_at < t1_at):
        first_barrier = "STOP_LOSS"
        status = "STOP_LOSS_FIRST"
    elif t1_at is not None and (stop_at is None or t1_at < stop_at):
        first_barrier = "T1"
        if before_stop:
            highest = before_stop[-1]
            status = f"{highest}_THEN_STOP" if stop_at is not None else f"{highest}_REACHED"
        else:
            status = "T1_REACHED"
    else:
        first_barrier = None
        status = (
            "SESSION_END_NO_LEVEL_TOUCHED"
            if now_utc >= close_utc
            else "MONITORING_NO_LEVEL_TOUCHED"
        )

    return {
        "status": status,
        "first_barrier": first_barrier,
        "quote_count": len(clean),
        "targets_touched_anytime": touched,
        "targets_before_stop": before_stop,
        "targets_after_stop": after_stop,
        "target_times": {
            name: _iso_utc(ts) for name, ts in target_times.items()
        },
        "stop_loss_at": _iso_utc(stop_at),
        "last_quote_at": _iso_utc(clean[-1][0]),
        "last_quote_price": round(clean[-1][1], 2),
        "quote_source_policy": "Exact match to entry snapshot price_source",
        "note": (
            "Sampled recorded quotes only. Targets first crossed after a "
            "stop are reported separately and are not counted as targets "
            "reached before the stop. Intrabar touches between quote samples "
            "cannot be confirmed."
        ),
    }


async def _replay_one(session, row, now_utc):
    event_ts = _utc_naive(getattr(row, "timestamp", None))
    if event_ts is None:
        return {"status": "INVALID_EVENT_TIMESTAMP"}

    close_utc = _session_close_utc(event_ts)
    if event_ts >= close_utc:
        return {"status": "EVENT_AFTER_SESSION_CLOSE"}

    levels, error = _validated_levels(row)
    if error:
        return {"status": "LEVELS_UNAVAILABLE", "reason": error}

    snapshot = _snapshot(row)
    source = str(snapshot.get("price_source") or "").strip()
    if not source:
        return {
            "status": "ENTRY_SOURCE_UNKNOWN",
            "reason": "Entry snapshot has no price_source; source-matched replay skipped",
        }

    expiry = canonical_expiry(getattr(row, "expiry", "") or "")
    option_type = str(getattr(row, "option_type", "") or "").upper().strip()
    strike = _positive(getattr(row, "strike", None))
    if not expiry or not option_type or strike is None:
        return {"status": "CONTRACT_DETAILS_UNAVAILABLE"}

    cutoff = min(now_utc, close_utc)
    stmt = (
        select(OptionQuoteHistory.recorded_at, OptionQuoteHistory.last_price)
        .where(
            OptionQuoteHistory.symbol == str(row.symbol).upper(),
            expiry_matches(OptionQuoteHistory.expiry, expiry),
            OptionQuoteHistory.strike == strike,
            func.upper(OptionQuoteHistory.option_type) == option_type,
            OptionQuoteHistory.quote_source == source,
            OptionQuoteHistory.recorded_at > event_ts,
            OptionQuoteHistory.recorded_at <= cutoff,
        )
        .order_by(OptionQuoteHistory.recorded_at.asc())
        .limit(MAX_QUOTE_ROWS_PER_CONTRACT)
    )
    result = await session.execute(stmt)
    quotes = [(item[0], item[1]) for item in result.all()]

    evaluation = _evaluate_quote_path(
        str(row.action).upper().strip(),
        levels,
        quotes,
        now_utc,
        close_utc,
    )
    evaluation["entry_price"] = levels["entry"]
    evaluation["levels"] = {
        "SL": levels["SL"], "T1": levels["T1"],
        "T2": levels["T2"], "T3": levels["T3"],
    }
    evaluation["entry_source"] = source
    evaluation["session_close_at"] = _iso_utc(close_utc)
    return evaluation


async def compute_call_tracker(symbol="ALL", days=15):
    """Read saved ledger entries and replay matching quotes without DB writes."""
    symbol = str(symbol or "ALL").upper().strip()
    if symbol != "ALL" and symbol not in VALID_SYMBOLS:
        raise ValueError("Unsupported symbol")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    since = now_utc - timedelta(days=int(days))

    stmt = (
        select(DailySignalLedger)
        .where(DailySignalLedger.timestamp >= since)
        .where(DailySignalLedger.timestamp <= now_utc)
        .order_by(DailySignalLedger.timestamp.desc(), DailySignalLedger.id.desc())
        .limit(MAX_LEDGER_ROWS)
    )
    if symbol != "ALL":
        stmt = stmt.where(DailySignalLedger.symbol == symbol)

    async with AsyncSessionLocal() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()
        calls = []

        for row in rows:
            snapshot = _snapshot(row)
            decision = _decision(row)
            gate = decision.get("entry_gate")
            if not isinstance(gate, dict):
                gate = {}

            classification = _entry_classification(row)
            evaluation = (
                await _replay_one(session, row, now_utc)
                if classification == "FRESH_ENTRY_ALLOWED"
                else {
                    "status": "NOT_GRADED_NON_FRESH_ENTRY",
                    "reason": "This record is not a verified fresh-entry-allowed call.",
                }
            )

            calls.append({
                "id": row.id,
                "timestamp": _iso_utc(row.timestamp),
                "symbol": str(row.symbol or "").upper(),
                "action": str(row.action or "").upper(),
                "lifecycle": str(row.lifecycle or "UNKNOWN"),
                "fresh_entry_status": classification,
                "option_type": str(row.option_type or "").upper(),
                "strike": row.strike,
                "expiry": row.expiry,
                "spot": row.spot,
                "entry_price": row.entry_price,
                "levels": _saved_level_display(row),
                "option_ltp_snapshot": row.option_ltp_snapshot,
                "entry_source": snapshot.get("price_source"),
                "bid": snapshot.get("bid"),
                "ask": snapshot.get("ask"),
                "signal_strength": row.signal_strength,
                "confidence": row.confidence,
                "decision_reason": str(
                    gate.get("reason")
                    or "No frozen entry-gate reason is available."
                ),
                "evaluation": evaluation,
                "actual_trade": False,
            })

    summary = {
        "records": len(calls),
        "fresh_entry_allowed": sum(
            c["fresh_entry_status"] == "FRESH_ENTRY_ALLOWED" for c in calls
        ),
        "holds": sum(
            c["fresh_entry_status"] == "HOLD_EXISTING_DIRECTION" for c in calls
        ),
        "denied": sum(
            c["fresh_entry_status"] == "ENTRY_DENIED" for c in calls
        ),
        "unknown": sum(
            c["fresh_entry_status"] in (
                "ENTRY_AUTHORITY_UNKNOWN", "ENTRY_AUTHORITY_INCONSISTENT"
            ) for c in calls
        ),
        "t1_before_stop": sum(
            "T1" in (c["evaluation"].get("targets_before_stop") or [])
            for c in calls
        ),
        "stop_loss_first": sum(
            c["evaluation"].get("first_barrier") == "STOP_LOSS"
            for c in calls
        ),
        "no_matching_quotes": sum(
            c["evaluation"].get("status") in (
                "NO_MATCHING_SOURCE_QUOTES", "ENTRY_SOURCE_UNKNOWN"
            ) for c in calls
        ),
    }

    return {
        "symbol": symbol,
        "days": int(days),
        "count": len(calls),
        "truncated": len(calls) >= MAX_LEDGER_ROWS,
        "summary": summary,
        "calls": calls,
        "basis": (
            "Read-only DailySignalLedger replay with same-contract, "
            "same-entry-source recorded quotes within the event's NSE session. "
            "Level touches are estimated from sampled quotes, not executed "
            "broker trades or realized P&L."
        ),
    }
