from app.utils.expiry import canonical_expiry, expiry_matches, expiry_storage_variants
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, and_, or_, union_all

from app.database import AsyncSessionLocal
from app.models import DailySignalLedger, MarketData, OptionData
from app.services.signal_accuracy import (
    ESTIMATED_ROUND_TRIP_COST_PCT,
    HORIZONS_MINUTES,
    SPOT_HORIZON_TOLERANCE_MIN,
    _asof_price,
    _grade,
    _grade_premium,
    _target_within_nse_session,
)
from app.utils.helpers import now_utc_naive

LEDGER_TOLERANCE_MIN = 10
EPISODE_GAP_MIN = 10

_ACTION_TO_SIDE = {
    "CALL_BUY": "CALL",
    "PUT_SELL": "CALL",
    "PUT_BUY": "PUT",
    "CALL_SELL": "PUT",
}


def _event_ts(row) -> Optional[datetime]:
    value = row.market_snapshot_timestamp or row.timestamp
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _horizon_in_session(event_ts: datetime, horizon_minutes: int) -> bool:
    """Require the complete evaluation horizon to stay inside one NSE session."""
    return _target_within_nse_session(
        event_ts, event_ts + timedelta(minutes=horizon_minutes)
    )


def _action(action: str) -> str:
    return {
        "BUY CE": "CALL_BUY",
        "BUY PE": "PUT_BUY",
        "SELL CE": "CALL_SELL",
        "SELL PE": "PUT_SELL",
    }.get(str(action or "").upper().strip(), str(action or "").upper().replace(" ", "_"))


def _episode_side(row) -> Optional[str]:
    action_side = _ACTION_TO_SIDE.get(_action(row.action))
    lifecycle = str(row.lifecycle or "").upper().strip()

    if action_side is None:
        return None

    if not lifecycle.startswith(("CONFIRMED_", "HOLD_")):
        return None

    if lifecycle.endswith("_CALL"):
        lifecycle_side = "CALL"
    elif lifecycle.endswith("_PUT"):
        lifecycle_side = "PUT"
    else:
        return None

    if lifecycle_side != action_side:
        return None

    return action_side


def _entry_classification(row) -> str:
    # Use the same frozen-authority rules as Call Tracker.
    from app.services.call_tracker import _entry_classification as classify
    return classify(row)


def _fresh_entry_status(row) -> Optional[bool]:
    """Return True only for validated V2 approval; preserve V1 denials/HOLDs."""
    classification = _entry_classification(row)
    if classification == "FRESH_ENTRY_ALLOWED":
        return True
    if classification in ("ENTRY_DENIED", "HOLD_EXISTING_DIRECTION"):
        return False
    return None


def _fresh_entry_is_hold(row) -> bool:
    return _entry_classification(row) == "HOLD_EXISTING_DIRECTION"


def _fresh_entry_is_legacy_risk_unverified(row) -> bool:
    return _entry_classification(row) == "LEGACY_RISK_UNVERIFIED"


def _independent_episode_rows(rows):
    episodes = []
    last_side = None
    last_ts = None

    for row in rows:
        ts = _event_ts(row)
        side = _episode_side(row)

        if ts is None or side is None:
            continue

        is_new_episode = (
            last_side != side
            or last_ts is None
            or (ts - last_ts).total_seconds() > EPISODE_GAP_MIN * 60
        )

        if is_new_episode:
            episodes.append(row)

        last_side = side
        last_ts = ts

    return episodes



def _independent_contract_episode_rows(rows):
    """Group repeated signals by side, action and option contract."""
    selected = []
    last_seen = {}

    for row in sorted(rows, key=lambda r: _event_ts(r) or datetime.max):
        ts = _event_ts(row)
        side = _episode_side(row)
        if ts is None or side is None:
            continue

        try:
            strike = round(float(row.strike), 6)
        except (TypeError, ValueError, OverflowError):
            continue

        expiry = canonical_expiry(row.expiry)
        opt = str(row.option_type or "").strip().upper()
        if not expiry or not opt:
            continue

        key = (side, _action(row.action), expiry, strike, opt)
        previous = last_seen.get(key)

        if previous is None or (
            ts - previous
        ).total_seconds() > EPISODE_GAP_MIN * 60:
            selected.append(row)

        last_seen[key] = ts

    return selected


def _trade_return(action: str, raw_pct: float) -> float:
    a = _action(action)
    return -raw_pct if a in ("PUT_BUY", "CALL_SELL") else raw_pct


def _premium_return(action: str, raw_pct: float) -> float:
    return -raw_pct if _action(action) in ("CALL_SELL", "PUT_SELL") else raw_pct


def _pnl_per_lot(entry_price: float, exit_price: float, lot_size: int, action: str) -> float:
    change = float(exit_price) - float(entry_price)
    if _action(action) in ("CALL_SELL", "PUT_SELL"):
        change = -change
    return change * int(lot_size)


def _summary(values: List[float]) -> Dict:
    if not values:
        return {"count": 0, "average_pct": None, "sum_pct": None,
                "min_pct": None, "max_pct": None}
    return {
        "count": len(values),
        "average_pct": round(sum(values) / len(values), 4),
        "sum_pct": round(sum(values), 4),
        "min_pct": round(min(values), 4),
        "max_pct": round(max(values), 4),
    }


def _grades(values: List[str]) -> Dict:
    c = sum(x == "correct" for x in values)
    w = sum(x == "wrong" for x in values)
    f = sum(x == "flat" for x in values)
    graded = c + w
    return {
        "correct": c,
        "wrong": w,
        "flat": f,
        "total_graded": graded,
        "accuracy_pct": round(c / graded * 100, 1) if graded else None,
    }


def _raw_expiry_matches(raw_value, requested_expiry):
    """Mirror expiry_matches() for one already-stored raw expiry value."""
    canonical = canonical_expiry(requested_expiry)
    if not canonical:
        return raw_value == requested_expiry

    # Match expiry_sql_key(): TRIM, remove hyphens/spaces, uppercase.
    normalized = (
        str(raw_value or "").strip(" ")
        .replace("-", "")
        .replace(" ", "")
        .upper()
    )
    return (
        normalized == canonical
        or raw_value in expiry_storage_variants(requested_expiry)
    )


def _build_option_history_statement(
    symbol, option_cutoff, contracts, stored_expiries
):
    """Use the contract/time composite index while preserving expiry aliases."""
    raw_expiries = tuple(value for value in stored_expiries if value is not None)
    branches = []

    for expiry, strike, option_type in sorted(contracts):
        matching_expiries = tuple(
            raw for raw in raw_expiries
            if _raw_expiry_matches(raw, expiry)
        )
        if not matching_expiries:
            continue

        branches.append(
            select(OptionData)
            .where(OptionData.symbol == symbol)
            .where(OptionData.expiry.in_(matching_expiries))
            .where(OptionData.strike == strike)
            .where(OptionData.option_type == option_type)
            .where(OptionData.timestamp >= option_cutoff)
        )

    if not branches:
        return None

    # No global ORDER BY: callers sort each contract's individual series.
    return select(OptionData).from_statement(union_all(*branches))


def _deduplicate_option_rows(rows):
    """Restore OR-query row uniqueness if legacy expiry aliases overlap."""
    by_id = {}
    rows_without_id = []
    for row in rows:
        row_id = getattr(row, "id", None)
        if row_id is None:
            rows_without_id.append(row)
        else:
            by_id.setdefault(row_id, row)
    return list(by_id.values()) + rows_without_id


async def compute_ledger_performance(
    symbol: str,
    days: int = 15,
    horizon_minutes: int = 60,
) -> Dict:
    if horizon_minutes not in HORIZONS_MINUTES:
        horizon_minutes = 60

    now = now_utc_naive()
    cutoff = now - timedelta(days=days)

    async with AsyncSessionLocal() as session:
        ledger = (
            await session.execute(
                select(DailySignalLedger)
                .where(DailySignalLedger.symbol == symbol)
                .where(DailySignalLedger.timestamp >= cutoff)
                .order_by(DailySignalLedger.timestamp.asc())
            )
        ).scalars().all()

        raw_ledger_rows = len(ledger)

        # Fresh-entry episodes are selected independently from HOLD/legacy
        # rows so an earlier non-entry observation cannot suppress a later
        # explicitly approved fresh entry for the same contract.
        fresh_statuses = [_fresh_entry_status(row) for row in ledger]
        legacy_risk_unverified = [
            _fresh_entry_is_legacy_risk_unverified(row) for row in ledger
        ]
        fresh_decision_metadata_rows = sum(
            status is not None or legacy
            for status, legacy in zip(fresh_statuses, legacy_risk_unverified)
        )
        fresh_entry_allowed_rows = sum(
            status is True for status in fresh_statuses
        )
        fresh_entry_hold_rows = sum(
            status is False and _fresh_entry_is_hold(row)
            for row, status in zip(ledger, fresh_statuses)
        )
        fresh_entry_denied_rows = sum(
            status is False and not _fresh_entry_is_hold(row)
            for row, status in zip(ledger, fresh_statuses)
        )
        fresh_entry_legacy_unverified_rows = sum(legacy_risk_unverified)
        fresh_entry_unknown_rows = sum(
            status is None and not legacy
            for status, legacy in zip(fresh_statuses, legacy_risk_unverified)
        )
        fresh_entry_rows = [
            row for row, status in zip(ledger, fresh_statuses)
            if status is True
        ]
        fresh_entry_contract_ledger = _independent_contract_episode_rows(
            fresh_entry_rows
        )

        contract_ledger = _independent_contract_episode_rows(ledger)
        ledger = _independent_episode_rows(ledger)

        market = (
            await session.execute(
                select(MarketData)
                .where(MarketData.symbol == symbol)
                .where(
                    MarketData.timestamp >=
                    cutoff - timedelta(minutes=LEDGER_TOLERANCE_MIN)
                )
                .order_by(MarketData.timestamp.asc())
            )
        ).scalars().all()

        contracts = {
            (
                canonical_expiry(r.expiry),
                round(float(r.strike), 6),
                str(r.option_type or "").upper().strip(),
            )
            for r in contract_ledger
            if r.expiry and r.strike is not None and r.option_type
        }

        options = []
        if contracts:
            option_cutoff = (
                cutoff - timedelta(minutes=LEDGER_TOLERANCE_MIN)
            )
            # Resolve the actual stored expiry spellings in this same
            # symbol/time window. The helper below retains legacy matcher
            # semantics without wrapping the indexed expiry column in SQL
            # functions for every contract predicate.
            stored_expiries = (
                await session.execute(
                    select(OptionData.expiry)
                    .where(OptionData.symbol == symbol)
                    .where(OptionData.timestamp >= option_cutoff)
                    .where(OptionData.expiry.is_not(None))
                    .distinct()
                )
            ).scalars().all()

            option_stmt = _build_option_history_statement(
                symbol, option_cutoff, contracts, stored_expiries
            )
            if option_stmt is not None:
                fetched_options = (
                    await session.execute(option_stmt)
                ).scalars().all()
                # UNION ALL can overlap for unusual, unparseable legacy
                # expiry aliases. The former OR query returned each row once.
                options = _deduplicate_option_rows(fetched_options)

    spot_series = [
        (r.timestamp, float(r.price))
        for r in market
        if r.timestamp is not None and r.price is not None and float(r.price) > 0
    ]
    spot_ts = [x[0] for x in spot_series]

    option_series: Dict[Tuple[str, float, str], List[Tuple[datetime, float]]] = {}
    for r in options:
        if r.timestamp is None or r.last_price is None:
            continue
        price = float(r.last_price)
        if price <= 0:
            continue
        key = (
            canonical_expiry(r.expiry),
            round(float(r.strike), 6),
            str(r.option_type or "").upper().strip(),
        )
        option_series.setdefault(key, []).append((r.timestamp, price))

    for values in option_series.values():
        values.sort(key=lambda x: x[0])

    direction_raw, direction_adj, points, direction_grades = [], [], [], []
    premium_raw, premium_adj, premium_net = [], [], []
    premium_grades, premium_net_grades = [], []
    gross_pnl, net_pnl = [], []

    episodes = []
    contract_episode_results = []
    matured = pending = spot_available = invalid = 0
    session_unavailable = 0
    contract_matured = contract_pending = contract_invalid = 0
    contract_session_unavailable = 0
    option_available = pnl_ready = missing_lot = 0

    for r in ledger:
        ts = _event_ts(r)
        if ts is None:
            invalid += 1
            continue

        target = ts + timedelta(minutes=horizon_minutes)
        snapshot = r.entry_snapshot if isinstance(r.entry_snapshot, dict) else {}

        entry_spot = float(r.spot) if r.spot and float(r.spot) > 0 else None
        entry_premium = None
        for value in (r.entry_price, r.option_ltp_snapshot):
            try:
                if value is not None and float(value) > 0:
                    entry_premium = float(value)
                    break
            except (TypeError, ValueError):
                pass

        lot = None
        try:
            value = snapshot.get("lot_size")
            if value is not None and int(value) > 0:
                lot = int(value)
        except (TypeError, ValueError):
            pass

        item = {
            "id": r.id,
            "timestamp": ts.isoformat(),
            "target_timestamp": target.isoformat(),
            "action": r.action,
            "option_type": r.option_type,
            "strike": r.strike,
            "expiry": r.expiry,
            "entry_spot": entry_spot,
            "entry_premium": entry_premium,
            "lot_size": lot,
        }

        if not _horizon_in_session(ts, horizon_minutes):
            session_unavailable += 1
            item["status"] = "session_unavailable"
            item["session_unavailable_reason"] = (
                "target_not_within_same_nse_session"
            )
            episodes.append(item)
            continue

        if target > now:
            pending += 1
            item["status"] = "pending"
            episodes.append(item)
            continue

        matured += 1
        later_spot = _asof_price(
            spot_series, spot_ts, target, SPOT_HORIZON_TOLERANCE_MIN
        )

        if entry_spot and later_spot:
            raw = (later_spot - entry_spot) / entry_spot * 100
            adj = _trade_return(r.action, raw)
            grade = _grade(_action(r.action), raw)
            direction_raw.append(raw)
            direction_adj.append(adj)
            points.append(later_spot - entry_spot)
            direction_grades.append(grade)
            spot_available += 1
            item.update({
                "spot_target": later_spot,
                "spot_raw_return_pct": round(raw, 4),
                "spot_action_adjusted_return_pct": round(adj, 4),
                "spot_points": round(later_spot - entry_spot, 4),
                "spot_grade": grade,
            })

        item["status"] = (
            "graded" if later_spot is not None else "no_data"
        )
        episodes.append(item)


    # Grade option premium and P&L on independent contract episodes.
    for r in contract_ledger:
        ts = _event_ts(r)
        if ts is None:
            contract_invalid += 1
            continue

        target = ts + timedelta(minutes=horizon_minutes)
        snapshot = r.entry_snapshot if isinstance(r.entry_snapshot, dict) else {}
        entry_premium = None
        for value in (r.entry_price, r.option_ltp_snapshot):
            try:
                if value is not None and float(value) > 0:
                    entry_premium = float(value)
                    break
            except (TypeError, ValueError):
                pass

        lot = None
        try:
            value = snapshot.get("lot_size")
            if value is not None and int(value) > 0:
                lot = int(value)
        except (TypeError, ValueError):
            pass
        if lot is None:
            missing_lot += 1

        item = {
            "id": r.id,
            "timestamp": ts.isoformat(),
            "target_timestamp": target.isoformat(),
            "action": r.action,
            "option_type": r.option_type,
            "strike": r.strike,
            "expiry": r.expiry,
            "entry_premium": entry_premium,
            "lot_size": lot,
        }

        if not _horizon_in_session(ts, horizon_minutes):
            contract_session_unavailable += 1
            item["status"] = "session_unavailable"
            item["session_unavailable_reason"] = (
                "target_not_within_same_nse_session"
            )
            contract_episode_results.append(item)
            continue

        if target > now:
            contract_pending += 1
            item["status"] = "pending"
            contract_episode_results.append(item)
            continue

        contract_matured += 1
        key = (
            canonical_expiry(r.expiry),
            round(float(r.strike), 6),
            str(r.option_type or "").upper().strip(),
        )
        series = option_series.get(key, [])
        later_premium = (
            _asof_price(
                series, [x[0] for x in series],
                target, LEDGER_TOLERANCE_MIN
            ) if series else None
        )

        if entry_premium and later_premium:
            raw = (later_premium - entry_premium) / entry_premium * 100
            adj = _premium_return(r.action, raw)
            net = adj - ESTIMATED_ROUND_TRIP_COST_PCT
            gross_grade = _grade_premium(adj)
            net_grade = _grade_premium(
                adj, ESTIMATED_ROUND_TRIP_COST_PCT
            )

            premium_raw.append(raw)
            premium_adj.append(adj)
            premium_net.append(net)
            premium_grades.append(gross_grade)
            premium_net_grades.append(net_grade)
            option_available += 1

            item.update({
                "premium_target": later_premium,
                "premium_raw_return_pct": round(raw, 4),
                "premium_action_adjusted_return_pct": round(adj, 4),
                "premium_net_return_pct": round(net, 4),
                "premium_gross_grade": gross_grade,
                "premium_net_grade": net_grade,
            })

            if lot is not None:
                g = _pnl_per_lot(
                    entry_premium,
                    later_premium,
                    lot,
                    r.action,
                )
                n = g - (
                    entry_premium
                    * ESTIMATED_ROUND_TRIP_COST_PCT / 100
                    * lot
                )
                gross_pnl.append(g)
                net_pnl.append(n)
                pnl_ready += 1
                item["gross_pnl_per_lot"] = round(g, 2)
                item["estimated_net_pnl_per_lot"] = round(n, 2)


        item["status"] = (
            "graded"
            if entry_premium and later_premium
            else "no_data"
        )
        contract_episode_results.append(item)

    # Separate replay using only rows whose frozen metadata explicitly says
    # fresh_entry_allowed=True. Keep existing all-episode metrics unchanged.
    fresh_premium_raw, fresh_premium_adj, fresh_premium_net = [], [], []
    fresh_premium_grades, fresh_premium_net_grades = [], []
    fresh_gross_pnl, fresh_net_pnl = [], []
    fresh_entry_contract_episode_results = []
    fresh_contract_matured = fresh_contract_pending = fresh_contract_invalid = 0
    fresh_contract_session_unavailable = 0
    fresh_option_available = fresh_pnl_ready = fresh_missing_lot = 0

    for r in fresh_entry_contract_ledger:
        ts = _event_ts(r)
        if ts is None:
            fresh_contract_invalid += 1
            fresh_entry_contract_episode_results.append({
                "id": r.id,
                "action": r.action,
                "status": "invalid_timestamp",
            })
            continue

        target = ts + timedelta(minutes=horizon_minutes)
        snapshot = r.entry_snapshot if isinstance(r.entry_snapshot, dict) else {}
        entry_premium = None
        for value in (r.entry_price, r.option_ltp_snapshot):
            try:
                if value is not None and float(value) > 0:
                    entry_premium = float(value)
                    break
            except (TypeError, ValueError):
                pass

        lot = None
        try:
            value = snapshot.get("lot_size")
            if value is not None and int(value) > 0:
                lot = int(value)
        except (TypeError, ValueError):
            pass
        if lot is None:
            fresh_missing_lot += 1

        item = {
            "id": r.id,
            "timestamp": ts.isoformat(),
            "target_timestamp": target.isoformat(),
            "action": r.action,
            "option_type": r.option_type,
            "strike": r.strike,
            "expiry": r.expiry,
            "entry_premium": entry_premium,
            "lot_size": lot,
        }

        if not _horizon_in_session(ts, horizon_minutes):
            fresh_contract_session_unavailable += 1
            item["status"] = "session_unavailable"
            item["session_unavailable_reason"] = (
                "target_not_within_same_nse_session"
            )
            fresh_entry_contract_episode_results.append(item)
            continue

        if target > now:
            fresh_contract_pending += 1
            item["status"] = "pending"
            fresh_entry_contract_episode_results.append(item)
            continue

        fresh_contract_matured += 1
        key = (
            canonical_expiry(r.expiry),
            round(float(r.strike), 6),
            str(r.option_type or "").upper().strip(),
        )
        series = option_series.get(key, [])
        later_premium = (
            _asof_price(
                series, [x[0] for x in series],
                target, LEDGER_TOLERANCE_MIN
            ) if series else None
        )

        if entry_premium and later_premium:
            raw = (later_premium - entry_premium) / entry_premium * 100
            adj = _premium_return(r.action, raw)
            net = adj - ESTIMATED_ROUND_TRIP_COST_PCT
            gross_grade = _grade_premium(adj)
            net_grade = _grade_premium(
                adj, ESTIMATED_ROUND_TRIP_COST_PCT
            )

            fresh_premium_raw.append(raw)
            fresh_premium_adj.append(adj)
            fresh_premium_net.append(net)
            fresh_premium_grades.append(gross_grade)
            fresh_premium_net_grades.append(net_grade)
            fresh_option_available += 1

            item.update({
                "premium_target": later_premium,
                "premium_raw_return_pct": round(raw, 4),
                "premium_action_adjusted_return_pct": round(adj, 4),
                "premium_net_return_pct": round(net, 4),
                "premium_gross_grade": gross_grade,
                "premium_net_grade": net_grade,
            })

            if lot is not None:
                gross = _pnl_per_lot(
                    entry_premium, later_premium, lot, r.action
                )
                fresh_net_value = gross - (
                    entry_premium
                    * ESTIMATED_ROUND_TRIP_COST_PCT / 100
                    * lot
                )
                fresh_gross_pnl.append(gross)
                fresh_net_pnl.append(fresh_net_value)
                fresh_pnl_ready += 1
                item["gross_pnl_per_lot"] = round(gross, 2)
                item["estimated_net_pnl_per_lot"] = round(
                    fresh_net_value, 2
                )

        item["status"] = (
            "graded"
            if entry_premium and later_premium
            else "no_data"
        )
        fresh_entry_contract_episode_results.append(item)

    return {
        "symbol": symbol,
        "days": days,
        "horizon_minutes": horizon_minutes,
        "basis": (
            "DailySignalLedger read-only replay. Spot direction uses the "
            "first row per independent 10-minute same-side episode. "
            "Premium/P&L uses independent episodes per side/action/contract. "
            "No future snapshot is used."
        ),
        "ledger_rows": raw_ledger_rows,
        "actionable_episodes": len(ledger),
        "contract_episodes": len(contract_ledger),
        "matured_rows": matured,
        "pending_rows": pending,
        "invalid_rows": invalid,
        "session_unavailable_rows": session_unavailable,
        "contract_matured_rows": contract_matured,
        "contract_pending_rows": contract_pending,
        "contract_invalid_rows": contract_invalid,
        "contract_session_unavailable_rows": contract_session_unavailable,
        "spot_data_available": spot_available,
        "option_data_available": option_available,
        "full_pnl_ready": pnl_ready,
        "missing_lot_size": missing_lot,
        "spot_direction": {
            "return_pct": _summary(direction_raw),
            "action_adjusted_return_pct": _summary(direction_adj),
            "points": {
                "count": len(points),
                "average": round(sum(points) / len(points), 4) if points else None,
                "sum": round(sum(points), 4) if points else None,
            },
            "grading": _grades(direction_grades),
        },
        "option_premium": {
            "raw_return_pct": _summary(premium_raw),
            "action_adjusted_return_pct": _summary(premium_adj),
            "estimated_net_return_pct": _summary(premium_net),
            "gross_grading": _grades(premium_grades),
            "net_of_estimated_cost_grading": _grades(premium_net_grades),
            "estimated_round_trip_cost_pct": ESTIMATED_ROUND_TRIP_COST_PCT,
        },
        "pnl": {
            "lot_size_source": "entry_snapshot.lot_size_only",
            "coverage_rows": pnl_ready,
            "gross_pnl_per_lot": {
                "sum": round(sum(gross_pnl), 2) if gross_pnl else None,
                "average": round(sum(gross_pnl) / len(gross_pnl), 2)
                if gross_pnl else None,
            },
            "estimated_net_pnl_per_lot": {
                "sum": round(sum(net_pnl), 2) if net_pnl else None,
                "average": round(sum(net_pnl) / len(net_pnl), 2)
                if net_pnl else None,
            },
        },
        "fresh_entry_performance": {
            "classification": (
                "Only metadata_version=2 rows with a validated matching "
                "entry_risk_authority approval enter fresh-entry performance. "
                "V1 positive rows are reported separately as legacy risk "
                "unverified. HOLD and denied rows remain separate; unknown "
                "rows are excluded."
            ),
            "metadata_version": 2,
            "ledger_rows_with_decision_metadata": fresh_decision_metadata_rows,
            "ledger_rows_fresh_entry_allowed": fresh_entry_allowed_rows,
            "ledger_rows_fresh_entry_denied": fresh_entry_denied_rows,
            "ledger_rows_hold_existing_direction": fresh_entry_hold_rows,
            "ledger_rows_legacy_risk_unverified": (
                fresh_entry_legacy_unverified_rows
            ),
            "ledger_rows_unclassified": fresh_entry_unknown_rows,
            "contract_episodes": len(fresh_entry_contract_ledger),
            "matured_rows": fresh_contract_matured,
            "pending_rows": fresh_contract_pending,
            "invalid_rows": fresh_contract_invalid,
            "session_unavailable_rows": fresh_contract_session_unavailable,
            "option_data_available": fresh_option_available,
            "full_pnl_ready": fresh_pnl_ready,
            "missing_lot_size": fresh_missing_lot,
            "option_premium": {
                "raw_return_pct": _summary(fresh_premium_raw),
                "action_adjusted_return_pct": _summary(fresh_premium_adj),
                "estimated_net_return_pct": _summary(fresh_premium_net),
                "gross_grading": _grades(fresh_premium_grades),
                "net_of_estimated_cost_grading": _grades(
                    fresh_premium_net_grades
                ),
                "estimated_round_trip_cost_pct": (
                    ESTIMATED_ROUND_TRIP_COST_PCT
                ),
            },
            "pnl": {
                "lot_size_source": "entry_snapshot.lot_size_only",
                "coverage_rows": fresh_pnl_ready,
                "gross_pnl_per_lot": {
                    "sum": round(sum(fresh_gross_pnl), 2)
                    if fresh_gross_pnl else None,
                    "average": round(
                        sum(fresh_gross_pnl) / len(fresh_gross_pnl), 2
                    ) if fresh_gross_pnl else None,
                },
                "estimated_net_pnl_per_lot": {
                    "sum": round(sum(fresh_net_pnl), 2)
                    if fresh_net_pnl else None,
                    "average": round(
                        sum(fresh_net_pnl) / len(fresh_net_pnl), 2
                    ) if fresh_net_pnl else None,
                },
            },
            "contract_episode_results": fresh_entry_contract_episode_results,
        },
        "episodes": episodes,
        "contract_episode_results": contract_episode_results,
    }
