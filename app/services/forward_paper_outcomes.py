"""Source-aware, read/write forward-paper outcome grading.

This module records observations only. It never places, simulates, or journals
broker orders. It deliberately leaves DailySignalLedger.outcome untouched.
"""
import math
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import (
    DailySignalLedger,
    ForwardPaperOutcome,
    MarketData,
    OptionQuoteHistory,
)
from app.services.call_tracker import (
    _entry_classification,
    _session_close_utc,
    _utc_naive,
)
from app.utils.expiry import canonical_expiry, expiry_matches


OUTCOME_HORIZONS_MINUTES = (5, 10, 15, 30, 60)
SPOT_TOLERANCE_MINUTES = 10
OPTION_TOLERANCE_MINUTES = 10
MAX_LEDGER_ROWS = 2000
MAX_QUOTES_PER_CONTRACT = 20000
IST = ZoneInfo("Asia/Kolkata")

_FINAL_STATUSES = {
    "matured",
    "matured_partial",
    "matured_no_data",
    "session_unavailable",
    "invalid_event_timestamp",
}


def _positive(value):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _valid_bid_ask(bid, ask):
    bid_value = _positive(bid)
    ask_value = _positive(ask)
    if bid_value is None or ask_value is None or ask_value < bid_value:
        return None, None
    return bid_value, ask_value


def _snapshot(row):
    value = getattr(row, "entry_snapshot", None)
    return value if isinstance(value, dict) else {}


def _capture(row):
    value = _snapshot(row).get("forward_paper_capture")
    return value if isinstance(value, dict) else {}


def _eligible(row):
    """Require matching V2 risk approvals in both frozen metadata records."""
    from app.services.entry_risk_authority import is_entry_risk_approved

    capture = _capture(row)
    snapshot = getattr(row, "entry_snapshot", None)
    if not isinstance(snapshot, dict):
        return False
    decision = snapshot.get("strategy_decision")
    if not isinstance(decision, dict):
        return False

    capture_authority = capture.get("entry_risk_authority")
    decision_authority = decision.get("entry_risk_authority")
    action = str(getattr(row, "action", "") or "").strip().upper()

    return bool(
        type(capture.get("schema_version")) is int
        and capture.get("schema_version") == 2
        and capture.get("eligible") is True
        and capture.get("actual_trade") is False
        and decision.get("metadata_version") == 2
        and is_entry_risk_approved(capture_authority)
        and is_entry_risk_approved(decision_authority)
        and capture_authority.get("assessment_id")
            == decision_authority.get("assessment_id")
        and capture_authority.get("action") == action
        and decision_authority.get("action") == action
        and _entry_classification(row) == "FRESH_ENTRY_ALLOWED"
    )



def _event_timestamp(row):
    return _utc_naive(getattr(row, "timestamp", None))


def _entry_source(snapshot, capture):
    quote = capture.get("quote")
    if not isinstance(quote, dict):
        quote = {}
    return str(
        snapshot.get("price_source")
        or quote.get("price_source")
        or ""
    ).strip() or None


def _entry_spot(row, snapshot, capture):
    value = _positive(getattr(row, "spot", None))
    if value is not None:
        return value
    spot = capture.get("spot")
    if isinstance(spot, dict):
        return _positive(spot.get("price"))
    return _positive(snapshot.get("spot"))


def _entry_price(row, snapshot, capture):
    quote = capture.get("quote")
    if not isinstance(quote, dict):
        quote = {}
    for value in (
        getattr(row, "entry_price", None),
        getattr(row, "option_ltp_snapshot", None),
        snapshot.get("entry_price"),
        quote.get("entry_price"),
        snapshot.get("ltp"),
        quote.get("ltp"),
    ):
        price = _positive(value)
        if price is not None:
            return price
    return None


def _entry_bid_ask(snapshot, capture):
    quote = capture.get("quote")
    if not isinstance(quote, dict):
        quote = {}
    bid, ask = _valid_bid_ask(
        snapshot.get("bid", quote.get("bid")),
        snapshot.get("ask", quote.get("ask")),
    )
    return bid, ask


def _action_factor(action):
    action = str(action or "").upper().strip()
    if action in ("BUY CE", "BUY PE"):
        return 1.0
    if action in ("SELL CE", "SELL PE"):
        return -1.0
    return None


def _spot_direction_factor(action):
    action = str(action or "").upper().strip()
    if action in ("BUY CE", "SELL PE"):
        return 1.0
    if action in ("BUY PE", "SELL CE"):
        return -1.0
    return None


def _adjusted_return(entry, exit_price, action):
    entry_value = _positive(entry)
    exit_value = _positive(exit_price)
    factor = _action_factor(action)
    if entry_value is None or exit_value is None or factor is None:
        return None, None
    raw = (exit_value - entry_value) / entry_value * 100.0
    return raw, raw * factor


def _execution_metrics(action, entry_bid, entry_ask, target_bid, target_ask, lot):
    entry_bid, entry_ask = _valid_bid_ask(entry_bid, entry_ask)
    target_bid, target_ask = _valid_bid_ask(target_bid, target_ask)
    factor = _action_factor(action)

    if (
        entry_bid is None or entry_ask is None
        or target_bid is None or target_ask is None
        or factor is None
    ):
        return None, None

    # BUY actions enter at ask and exit at bid. SELL actions enter at bid
    # and exit at ask. This is an estimate from saved quotes, not execution.
    entry_fill = entry_ask if action.startswith("BUY") else entry_bid
    exit_fill = target_bid if action.startswith("BUY") else target_ask

    if entry_fill <= 0 or exit_fill <= 0:
        return None, None

    adjusted_pct = (exit_fill - entry_fill) / entry_fill * 100.0 * factor
    lot_size = None
    try:
        if lot is not None and int(lot) > 0:
            lot_size = int(lot)
    except (TypeError, ValueError, OverflowError):
        pass

    pnl_per_lot = (
        (exit_fill - entry_fill) * lot_size * factor
        if lot_size is not None else None
    )
    return adjusted_pct, pnl_per_lot


def _session_valid(event_ts, horizon_minutes):
    close_utc = _session_close_utc(event_ts)
    local_event = event_ts.replace(tzinfo=timezone.utc).astimezone(IST)
    target_ts = event_ts + timedelta(minutes=horizon_minutes)

    if local_event.time() < dt_time(9, 15) or local_event.time() >= dt_time(15, 30):
        return False, target_ts, close_utc, "event_outside_nse_session"
    if target_ts > close_utc:
        return False, target_ts, close_utc, "target_outside_same_nse_session"
    return True, target_ts, close_utc, None


def _nearest_before_or_after(items, target_ts, event_ts, timestamp_getter):
    eligible = []
    for item in items:
        stamp = _utc_naive(timestamp_getter(item))
        if stamp is None or stamp <= event_ts:
            continue
        eligible.append((item, stamp))

    if not eligible:
        return None, None

    item, stamp = min(
        eligible,
        key=lambda pair: (
            abs((pair[1] - target_ts).total_seconds()),
            0 if pair[1] <= target_ts else 1,
            pair[1],
        ),
    )
    return item, stamp


def _reset_measurements(outcome, snapshot, capture, row):
    outcome.entry_source = _entry_source(snapshot, capture)
    outcome.option_entry_price = _entry_price(row, snapshot, capture)
    outcome.option_entry_bid, outcome.option_entry_ask = _entry_bid_ask(
        snapshot, capture
    )
    outcome.spot_entry_price = _entry_spot(row, snapshot, capture)


async def _grade_matured_horizon(session, ledger_row, outcome, event_ts, target_ts, close_utc, now_utc):
    snapshot = _snapshot(ledger_row)
    capture = _capture(ledger_row)
    action = str(ledger_row.action or "").upper().strip()
    symbol = str(ledger_row.symbol or "").upper().strip()
    expiry = canonical_expiry(ledger_row.expiry or "")
    strike = _positive(ledger_row.strike)
    option_type = str(ledger_row.option_type or "").upper().strip()
    source = _entry_source(snapshot, capture)
    entry_price = _entry_price(ledger_row, snapshot, capture)
    entry_spot = _entry_spot(ledger_row, snapshot, capture)
    entry_bid, entry_ask = _entry_bid_ask(snapshot, capture)

    outcome.entry_source = source
    outcome.option_entry_price = entry_price
    outcome.option_entry_bid = entry_bid
    outcome.option_entry_ask = entry_ask
    outcome.spot_entry_price = entry_spot

    spot_target = None
    spot_return = None
    spot_directional_return = None
    spot_status = "no_matching_spot_observation"
    spot_offset = None

    option_quote = None
    option_quote_ts = None
    option_status = "no_matching_source_quote"
    option_raw_return = None
    option_adjusted_return = None
    execution_return = None
    execution_pnl = None
    mfe = None
    mae = None
    path_count = 0

    spot_tolerance = timedelta(minutes=SPOT_TOLERANCE_MINUTES)
    spot_start = max(target_ts - spot_tolerance, event_ts)
    spot_end = min(target_ts + spot_tolerance, close_utc)

    spot_stmt = (
        select(MarketData)
        .where(
            MarketData.symbol == symbol,
            MarketData.timestamp > event_ts,
            MarketData.timestamp >= spot_start,
            MarketData.timestamp <= spot_end,
        )
        .order_by(MarketData.timestamp.asc())
        .limit(5000)
    )
    spot_rows = (await session.execute(spot_stmt)).scalars().all()
    spot_rows = [
        item for item in spot_rows
        if _positive(getattr(item, "price", None)) is not None
        and _utc_naive(getattr(item, "timestamp", None)) is not None
    ]
    selected_spot, selected_spot_ts = _nearest_before_or_after(
        spot_rows, target_ts, event_ts,
        lambda item: item.timestamp,
    )

    if selected_spot is not None:
        spot_target = _positive(selected_spot.price)
        spot_offset = (selected_spot_ts - target_ts).total_seconds()
        spot_status = "available"
        if _positive(entry_spot) is not None:
            spot_return = (spot_target - entry_spot) / entry_spot * 100.0
            direction_factor = _spot_direction_factor(action)
            if direction_factor is not None:
                spot_directional_return = spot_return * direction_factor

        outcome.spot_target_price = spot_target
        outcome.spot_target_observed_at = selected_spot_ts
        outcome.spot_target_offset_seconds = spot_offset
        outcome.spot_return_pct = spot_return
        outcome.spot_directional_return_pct = spot_directional_return
    else:
        outcome.spot_target_price = None
        outcome.spot_target_observed_at = None
        outcome.spot_target_offset_seconds = None
        outcome.spot_return_pct = None
        outcome.spot_directional_return_pct = None
        if _positive(entry_spot) is None:
            spot_status = "entry_spot_unavailable"

    if source and expiry and strike is not None and option_type in ("CE", "PE"):
        option_tolerance = timedelta(minutes=OPTION_TOLERANCE_MINUTES)
        option_start = max(target_ts - option_tolerance, event_ts)
        option_end = min(target_ts + option_tolerance, close_utc)

        option_stmt = (
            select(OptionQuoteHistory)
            .where(
                OptionQuoteHistory.symbol == symbol,
                expiry_matches(OptionQuoteHistory.expiry, expiry),
                OptionQuoteHistory.strike == strike,
                func.upper(OptionQuoteHistory.option_type) == option_type,
                OptionQuoteHistory.quote_source == source,
                OptionQuoteHistory.recorded_at > event_ts,
                OptionQuoteHistory.recorded_at >= option_start,
                OptionQuoteHistory.recorded_at <= option_end,
            )
            .order_by(OptionQuoteHistory.recorded_at.asc())
            .limit(MAX_QUOTES_PER_CONTRACT)
        )
        option_rows = (await session.execute(option_stmt)).scalars().all()
        option_rows = [
            item for item in option_rows
            if _utc_naive(getattr(item, "recorded_at", None)) is not None
            and (
                _positive(getattr(item, "last_price", None)) is not None
                or _valid_bid_ask(
                    getattr(item, "bid", None),
                    getattr(item, "ask", None),
                )[0] is not None
            )
        ]
        option_quote, option_quote_ts = _nearest_before_or_after(
            option_rows, target_ts, event_ts,
            lambda item: item.recorded_at,
        )

        if option_quote is not None:
            target_ltp = _positive(option_quote.last_price)
            target_bid, target_ask = _valid_bid_ask(
                option_quote.bid, option_quote.ask
            )
            option_offset = (option_quote_ts - target_ts).total_seconds()

            outcome.option_target_ltp = target_ltp
            outcome.option_target_bid = target_bid
            outcome.option_target_ask = target_ask
            outcome.option_target_recorded_at = option_quote_ts
            outcome.option_target_offset_seconds = option_offset
            outcome.option_quote_source = str(option_quote.quote_source or "")
            outcome.option_ws_received_at = option_quote.ws_received_at
            outcome.option_ws_exchange_timestamp = option_quote.ws_exchange_timestamp
            outcome.option_ws_exchange_timestamp_ms = option_quote.ws_exchange_timestamp_ms

            option_raw_return, option_adjusted_return = _adjusted_return(
                entry_price, target_ltp, action
            )
            execution_return, execution_pnl = _execution_metrics(
                action,
                entry_bid,
                entry_ask,
                target_bid,
                target_ask,
                _snapshot(ledger_row).get(
                    "lot_size",
                    capture.get("contract", {}).get("lot_size")
                    if isinstance(capture.get("contract"), dict) else None,
                ),
            )

            if target_ltp is not None:
                option_status = "available"
            elif target_bid is not None and target_ask is not None:
                option_status = "bid_ask_only"
            else:
                option_status = "target_quote_unusable"

            if entry_price is not None and target_ltp is not None:
                path_stmt = (
                    select(
                        OptionQuoteHistory.recorded_at,
                        OptionQuoteHistory.last_price,
                    )
                    .where(
                        OptionQuoteHistory.symbol == symbol,
                        expiry_matches(OptionQuoteHistory.expiry, expiry),
                        OptionQuoteHistory.strike == strike,
                        func.upper(OptionQuoteHistory.option_type) == option_type,
                        OptionQuoteHistory.quote_source == source,
                        OptionQuoteHistory.recorded_at > event_ts,
                        OptionQuoteHistory.recorded_at <= option_quote_ts,
                    )
                    .order_by(OptionQuoteHistory.recorded_at.asc())
                    .limit(MAX_QUOTES_PER_CONTRACT)
                )
                path_rows = (await session.execute(path_stmt)).all()
                path_returns = []
                for stamp, ltp in path_rows:
                    if _utc_naive(stamp) is None:
                        continue
                    _, adjusted = _adjusted_return(entry_price, ltp, action)
                    if adjusted is not None:
                        path_returns.append(adjusted)
                path_count = len(path_returns)
                if path_returns:
                    mfe = max(path_returns)
                    mae = min(path_returns)
        else:
            outcome.option_target_ltp = None
            outcome.option_target_bid = None
            outcome.option_target_ask = None
            outcome.option_target_recorded_at = None
            outcome.option_target_offset_seconds = None
            outcome.option_quote_source = source
            outcome.option_ws_received_at = None
            outcome.option_ws_exchange_timestamp = None
            outcome.option_ws_exchange_timestamp_ms = None
    else:
        if not source:
            option_status = "entry_source_unknown"
        else:
            option_status = "contract_details_unavailable"
        outcome.option_target_ltp = None
        outcome.option_target_bid = None
        outcome.option_target_ask = None
        outcome.option_target_recorded_at = None
        outcome.option_target_offset_seconds = None
        outcome.option_quote_source = source
        outcome.option_ws_received_at = None
        outcome.option_ws_exchange_timestamp = None
        outcome.option_ws_exchange_timestamp_ms = None

    outcome.premium_raw_return_pct = option_raw_return
    outcome.premium_action_adjusted_return_pct = option_adjusted_return
    outcome.execution_aware_return_pct = execution_return
    outcome.execution_aware_pnl_per_lot = execution_pnl
    outcome.mfe_pct = mfe
    outcome.mae_pct = mae
    outcome.option_quote_count = path_count

    spot_available = spot_return is not None
    option_mark_available = option_adjusted_return is not None
    execution_available = execution_return is not None
    available_components = sum(
        (spot_available, option_mark_available, execution_available)
    )

    if spot_available and option_mark_available:
        status = "matured"
        reason = "Spot and same-source option premium outcomes are available"
    elif available_components:
        status = "matured_partial"
        reason = "Horizon matured; one or more measurement components are unavailable"
    else:
        status = "matured_no_data"
        reason = "Horizon matured without a usable spot or option-return measurement"

    outcome.status = status
    outcome.status_reason = reason
    outcome.last_evaluated_at = now_utc
    outcome.details = {
        "schema_version": 1,
        "actual_trade": False,
        "entry_classification": "FRESH_ENTRY_ALLOWED",
        "spot_status": spot_status,
        "spot_source_policy": (
            "market_data_history; MarketData does not persist provider identity"
        ),
        "spot_tolerance_minutes": SPOT_TOLERANCE_MINUTES,
        "spot_offset_seconds": spot_offset,
        "option_status": option_status,
        "option_source_policy": "Exact match to frozen entry price_source",
        "option_tolerance_minutes": OPTION_TOLERANCE_MINUTES,
        "option_offset_seconds": (
            (option_quote_ts - target_ts).total_seconds()
            if option_quote_ts is not None else None
        ),
        "option_path_policy": (
            "MFE/MAE use matching-source LTP samples from after entry "
            "through the selected horizon quote; sampled values are not executions"
        ),
        "option_path_quote_count": path_count,
        "execution_aware_policy": (
            "BUY: ask entry/bid exit; SELL: bid entry/ask exit. "
            "Requires valid saved bid/ask on both ends."
        ),
        "target_timestamp": target_ts.isoformat(),
        "session_close_timestamp": close_utc.isoformat(),
        "target_quote_recorded_at": (
            option_quote_ts.isoformat() if option_quote_ts is not None else None
        ),
    }
    return outcome.status


async def process_forward_paper_outcomes(*, now=None, ledger_limit=MAX_LEDGER_ROWS):
    """Create pending horizon rows, then grade them once their quote window closes.

    A horizon is finalized only after its target tolerance window has elapsed.
    Same-session constraints cap that window at 15:30 IST. Finalized outcomes
    are immutable on later runs, making repeated scheduler calls idempotent.
    """
    now_utc = _utc_naive(now) if now is not None else None
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    cutoff = now_utc - timedelta(days=60)
    stmt = (
        select(DailySignalLedger)
        .where(
            DailySignalLedger.timestamp >= cutoff,
            DailySignalLedger.timestamp <= now_utc,
        )
        .order_by(DailySignalLedger.timestamp.desc(), DailySignalLedger.id.desc())
        .limit(max(1, min(int(ledger_limit), MAX_LEDGER_ROWS)))
    )

    stats = {
        "ledger_rows_seen": 0,
        "eligible_entries": 0,
        "outcome_rows_created": 0,
        "outcome_rows_updated": 0,
        "terminal_rows_skipped": 0,
        "pending_rows": 0,
        "matured_rows": 0,
        "session_unavailable_rows": 0,
        "legacy_pending_entries": 0,
    }

    async with AsyncSessionLocal() as session:
        ledger_rows = (await session.execute(stmt)).scalars().all()
        stats["ledger_rows_seen"] = len(ledger_rows)
        ledger_ids = [row.id for row in ledger_rows if row.id is not None]

        if not ledger_ids:
            return stats

        existing_rows = (
            await session.execute(
                select(ForwardPaperOutcome).where(
                    ForwardPaperOutcome.ledger_id.in_(ledger_ids)
                )
            )
        ).scalars().all()
        existing_by_key = {
            (item.ledger_id, item.horizon_minutes): item
            for item in existing_rows
        }

        changed = False

        # Oldest first helps a bounded batch make progress through the history.
        # Existing pending outcomes are graded even when their source ledger
        # row is legacy/unapproved; only approved V2 rows may create new ones.
        for ledger_row in reversed(ledger_rows):
            eligible = _eligible(ledger_row)
            existing_for_entry = [
                item for (ledger_id, _), item in existing_by_key.items()
                if ledger_id == ledger_row.id
            ]

            if not eligible and not existing_for_entry:
                continue

            if eligible:
                stats["eligible_entries"] += 1
            elif any(
                item.status not in _FINAL_STATUSES
                for item in existing_for_entry
            ):
                stats["legacy_pending_entries"] += 1

            event_ts = _event_timestamp(ledger_row)
            if event_ts is None:
                continue

            for horizon in OUTCOME_HORIZONS_MINUTES:
                key = (ledger_row.id, horizon)
                outcome = existing_by_key.get(key)
                was_created = outcome is None

                # Never backfill missing horizons for V1, denied, or otherwise
                # unapproved rows. An already-existing pending row is still
                # allowed through to the grading path below.
                if was_created and not eligible:
                    continue

                if outcome is not None and outcome.status in _FINAL_STATUSES:
                    stats["terminal_rows_skipped"] += 1
                    continue

                valid, target_ts, close_utc, session_reason = _session_valid(
                    event_ts, horizon
                )

                if was_created:
                    outcome = ForwardPaperOutcome(
                        ledger_id=ledger_row.id,
                        symbol=str(ledger_row.symbol or "").upper(),
                        horizon_minutes=horizon,
                        event_timestamp=event_ts,
                        target_timestamp=target_ts,
                        status="pending",
                        status_reason="Waiting for the horizon observation window",
                        details={
                            "schema_version": 1,
                            "actual_trade": False,
                            "state": "pending",
                        },
                    )
                    session.add(outcome)
                    existing_by_key[key] = outcome
                    stats["outcome_rows_created"] += 1
                    changed = True

                if not valid:
                    outcome.status = "session_unavailable"
                    outcome.status_reason = session_reason
                    outcome.last_evaluated_at = now_utc
                    outcome.details = {
                        "schema_version": 1,
                        "actual_trade": False,
                        "reason": session_reason,
                        "horizon_minutes": horizon,
                        "target_timestamp": target_ts.isoformat(),
                        "session_close_timestamp": close_utc.isoformat(),
                    }
                    stats["session_unavailable_rows"] += 1
                    if not was_created:
                        stats["outcome_rows_updated"] += 1
                    changed = True
                    continue

                sample_end = min(
                    target_ts + timedelta(minutes=max(
                        SPOT_TOLERANCE_MINUTES, OPTION_TOLERANCE_MINUTES
                    )),
                    close_utc,
                )
                if now_utc < sample_end:
                    # Do not repeatedly update already-pending rows.
                    stats["pending_rows"] += 1
                    continue

                final_status = await _grade_matured_horizon(
                    session,
                    ledger_row,
                    outcome,
                    event_ts,
                    target_ts,
                    close_utc,
                    now_utc,
                )
                stats["matured_rows"] += 1
                if not was_created:
                    stats["outcome_rows_updated"] += 1
                changed = True

        if changed:
            await session.commit()

    return stats
