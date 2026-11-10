from datetime import datetime, timedelta, timezone, time
from statistics import median
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select

from app.database import AsyncSessionLocal
from app.models import DailySignalLedger, MarketData, OptionData
from app.services.ledger_performance import (
    _action,
    _grade,
    _grade_premium,
    _premium_return,
    _trade_return,
)
from app.utils.helpers import now_utc_naive

IST = ZoneInfo("Asia/Kolkata")

LATE_CENTER_MINUTES = 14 * 60 + 40
LATE_TOLERANCE_MINUTES = 10
CLOSE_START = time(15, 20)
CLOSE_END = time(15, 30)
EXIT_1540 = time(15, 40)
EXIT_TOLERANCE_MINUTES = 10


def _to_ist(value) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None

    if not isinstance(value, datetime):
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(IST)


def _ledger_event_ist(row: DailySignalLedger) -> Optional[datetime]:
    return _to_ist(row.market_snapshot_timestamp) or _to_ist(row.timestamp)


def _pct(start: float, end: float) -> Optional[float]:
    if start is None or end is None or start <= 0:
        return None
    return (end - start) / start * 100.0


def _nearest_price(
    rows: List[Tuple[datetime, float]],
    target: datetime,
    tolerance_minutes: int = 5,
) -> Optional[float]:
    if not rows:
        return None

    tolerance = timedelta(minutes=tolerance_minutes)
    candidates = [
        (abs(ts - target), price)
        for ts, price in rows
        if abs(ts - target) <= tolerance
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _opening_profile(days: List[dict]) -> Dict:
    gaps = []
    boundary_moves = []
    continuation = reversal = neutral = 0

    for day in days:
        gap = day.get("gap_pct")
        boundary = day.get("boundary_move_pct")

        if gap is not None:
            gaps.append(gap)
        if boundary is not None:
            boundary_moves.append(boundary)

        if gap is None or boundary is None:
            neutral += 1
            continue

        if gap == 0 or boundary == 0:
            neutral += 1
        elif gap * boundary > 0:
            continuation += 1
        else:
            reversal += 1

    def summary(values):
        return {
            "count": len(values),
            "average_pct": round(sum(values) / len(values), 4) if values else None,
            "median_pct": round(median(values), 4) if values else None,
        }

    valid_gap_days = sum(
        1 for d in days if d.get("gap_pct") is not None
    )

    return {
        "days": len(days),
        "valid_gap_days": valid_gap_days,
        "coverage_pct": round(
            valid_gap_days / len(days) * 100, 1
        ) if days else None,
        "gap": summary(gaps),
        "open_boundary_09_15_to_09_30": summary(boundary_moves),
        "continuation": continuation,
        "reversal": reversal,
        "neutral_or_missing": neutral,
    }


def _outcome_label(grade: Optional[str]) -> str:
    return {
        "correct": "favourable",
        "wrong": "adverse",
        "flat": "flat",
    }.get(grade, "no_data")


def _aggregate_outcomes(items: List[dict], field: str) -> Dict:
    counts = {
        "favourable": 0,
        "adverse": 0,
        "flat": 0,
        "pending": 0,
        "no_data": 0,
    }
    values = []

    for item in items:
        outcome = item.get(field, "no_data")
        counts[outcome] = counts.get(outcome, 0) + 1

        pct_key = (
            "spot_action_adjusted_return_pct"
            if field == "spot_outcome"
            else "premium_action_adjusted_return_pct"
        )
        value = item.get(pct_key)
        if value is not None:
            values.append(value)

    return {
        **counts,
        "move": {
            "count": len(values),
            "average_pct": round(sum(values) / len(values), 4) if values else None,
            "median_pct": round(median(values), 4) if values else None,
        },
    }


async def compute_session_study(
    symbol: str,
    days: int = 30,
) -> Dict:
    symbol = str(symbol or "").upper().strip()
    if days < 1:
        days = 30

    now_utc = now_utc_naive()
    cutoff = now_utc - timedelta(days=days)
    cutoff_ist = cutoff.replace(tzinfo=timezone.utc).astimezone(IST)

    async with AsyncSessionLocal() as session:
        market_rows = (
            await session.execute(
                select(MarketData)
                .where(MarketData.symbol == symbol)
                .where(MarketData.timestamp >= cutoff - timedelta(days=3))
                .order_by(MarketData.timestamp.asc())
            )
        ).scalars().all()

        ledger_rows = (
            await session.execute(
                select(DailySignalLedger)
                .where(DailySignalLedger.symbol == symbol)
                .where(DailySignalLedger.timestamp >= cutoff)
                .order_by(DailySignalLedger.timestamp.asc())
            )
        ).scalars().all()

        contracts = {
            (
                str(r.expiry or "").strip(),
                round(float(r.strike), 6),
                str(r.option_type or "").upper().strip(),
            )
            for r in ledger_rows
            if r.expiry and r.strike is not None and r.option_type
        }

        option_rows = []
        if contracts:
            filters = [
                and_(
                    OptionData.expiry == expiry,
                    OptionData.strike == strike,
                    OptionData.option_type == opt,
                )
                for expiry, strike, opt in contracts
            ]

            option_rows = (
                await session.execute(
                    select(OptionData)
                    .where(OptionData.symbol == symbol)
                    .where(
                        OptionData.timestamp >= cutoff - timedelta(days=3)
                    )
                    .where(or_(*filters))
                    .order_by(OptionData.timestamp.asc())
                )
            ).scalars().all()

    market = []
    for row in market_rows:
        ts = _to_ist(row.timestamp)
        try:
            price = float(row.price)
        except (TypeError, ValueError):
            continue
        if ts is None or price <= 0:
            continue
        market.append((ts, price))

    session_map: Dict = {}
    for ts, price in market:
        if not (time(9, 15) <= ts.time() < time(15, 30)):
            continue
        session_map.setdefault(ts.date(), []).append((ts, price))

    all_trading_dates = sorted(session_map)

    # Warm-up history is retained so the first requested session can still
    # use the immediately preceding trading day's close for gap calculation.
    # Only sessions containing a snapshot at/after the requested IST cutoff
    # belong in the report itself.
    report_dates = [
        session_date
        for session_date in all_trading_dates
        if any(ts >= cutoff_ist for ts, _ in session_map[session_date])
    ]

    daily_profiles = []

    for session_date in report_dates:
        rows = sorted(session_map[session_date], key=lambda x: x[0])

        first_open = _nearest_price(
            rows,
            datetime.combine(session_date, time(9, 15), tzinfo=IST),
            tolerance_minutes=5,
        )

        boundary_0930 = _nearest_price(
            rows,
            datetime.combine(session_date, time(9, 30), tzinfo=IST),
            tolerance_minutes=5,
        )

        close_rows = [
            (ts, price)
            for ts, price in rows
            if CLOSE_START <= ts.time() <= CLOSE_END
        ]

        previous_close = None
        previous_dates = [
            d for d in all_trading_dates
            if d < session_date
        ]
        if previous_dates:
            previous_rows = session_map[previous_dates[-1]]
            if previous_rows:
                previous_close = previous_rows[-1][1]

        gap_pct = _pct(previous_close, first_open)
        boundary_move_pct = _pct(first_open, boundary_0930)

        close_start = close_rows[0][1] if close_rows else None
        close_end = close_rows[-1][1] if close_rows else None

        daily_profiles.append({
            "session_date": session_date.isoformat(),
            "previous_close": previous_close,
            "open_0915": first_open,
            "open_boundary_0930": boundary_0930,
            "gap_pct": gap_pct,
            "boundary_move_pct": boundary_move_pct,
            "close_1520": close_start,
            "close_1530": close_end,
            "closing_window_move_pct": _pct(close_start, close_end),
        })

    option_map: Dict[Tuple[str, float, str], List[Tuple[datetime, float]]] = {}
    for row in option_rows:
        ts = _to_ist(row.timestamp)
        try:
            price = float(row.last_price)
        except (TypeError, ValueError):
            continue
        if ts is None or price <= 0:
            continue
        key = (
            str(row.expiry or "").strip(),
            round(float(row.strike), 6),
            str(row.option_type or "").upper().strip(),
        )
        option_map.setdefault(key, []).append((ts, price))

    for series in option_map.values():
        series.sort(key=lambda x: x[0])

    late_calls = []

    for row in ledger_rows:
        event_ts = _ledger_event_ist(row)
        if event_ts is None:
            continue

        center = event_ts.replace(
            hour=14,
            minute=40,
            second=0,
            microsecond=0,
        )

        if abs(event_ts - center) > timedelta(minutes=LATE_TOLERANCE_MINUTES):
            continue

        date_rows = session_map.get(event_ts.date(), [])
        session_close = next(
            (
                price for ts, price in reversed(date_rows)
                if time(15, 20) <= ts.time() <= time(15, 30)
            ),
            None,
        )

        entry_spot = float(row.spot) if row.spot and row.spot > 0 else None
        entry_premium = None

        for value in (row.entry_price, row.option_ltp_snapshot):
            try:
                if value is not None and float(value) > 0:
                    entry_premium = float(value)
                    break
            except (TypeError, ValueError):
                pass

        item = {
            "id": row.id,
            "session_date": event_ts.date().isoformat(),
            "event_timestamp_ist": event_ts.isoformat(),
            "distance_from_1440_minutes": round(
                abs((event_ts - center).total_seconds()) / 60, 3
            ),
            "action": row.action,
            "option_type": row.option_type,
            "strike": row.strike,
            "expiry": row.expiry,
            "entry_spot": entry_spot,
            "entry_premium": entry_premium,
            "signal_strength": row.signal_strength,
            "lifecycle": row.lifecycle,
            "confirmations": row.confirmations,
            "technical_data_source": row.technical_data_source,
            "spot_outcome": "no_data",
            "premium_1540_outcome": "no_data",
        }

        if entry_spot and session_close:
            raw = _pct(entry_spot, session_close)
            grade = _grade(_action(row.action), raw)
            item["spot_raw_return_pct"] = round(raw, 4)
            item["spot_action_adjusted_return_pct"] = round(
                _trade_return(row.action, raw), 4
            )
            item["spot_outcome"] = _outcome_label(grade)
        else:
            today_ist = datetime.now(IST).date()
            if event_ts.date() == today_ist and datetime.now(IST).time() < EXIT_1540:
                item["spot_outcome"] = "pending"

        key = (
            str(row.expiry or "").strip(),
            round(float(row.strike), 6),
            str(row.option_type or "").upper().strip(),
        )
        series = option_map.get(key, [])

        exit_1540 = _nearest_price(
            series,
            datetime.combine(event_ts.date(), EXIT_1540, tzinfo=IST),
            tolerance_minutes=EXIT_TOLERANCE_MINUTES,
        )

        if entry_premium and exit_1540:
            raw = _pct(entry_premium, exit_1540)
            adjusted = _premium_return(row.action, raw)
            item["premium_1540_raw_return_pct"] = round(raw, 4)
            item["premium_action_adjusted_return_pct"] = round(adjusted, 4)
            item["premium_1540_outcome"] = _outcome_label(
                _grade_premium(adjusted)
            )
        else:
            now_ist = datetime.now(IST)
            if (
                event_ts.date() == now_ist.date()
                and now_ist.time() < EXIT_1540
            ):
                item["premium_1540_outcome"] = "pending"

        late_calls.append(item)

    market_days = len(daily_profiles)
    closing_valid = sum(
        1 for d in daily_profiles
        if d.get("closing_window_move_pct") is not None
    )

    return {
        "symbol": symbol,
        "days": days,
        "basis": (
            "Read-only Session Study using MarketData + DailySignalLedger "
            "+ selected OptionData; IST session-date normalized; "
            "no strategy score/gate/lifecycle mutation; "
            "no future snapshot is used."
        ),
        "method": {
            "session": "09:15-15:30 IST",
            "opening_gap": "previous trading-day close -> nearest 09:15 snapshot",
            "opening_boundary": "09:15 -> nearest 09:30 snapshot",
            "late_session_center": "14:40 IST",
            "late_session_tolerance_minutes": LATE_TOLERANCE_MINUTES,
            "session_close_window": "15:20-15:30 IST",
            "option_exit_window": "15:40 IST ± 10 minutes",
        },
        "market_days": market_days,
        "opening_profile": _opening_profile(daily_profiles),
        "closing_window": {
            "valid_days": closing_valid,
            "coverage_pct": round(
                closing_valid / market_days * 100, 1
            ) if market_days else None,
            "move_average_pct": round(
                sum(
                    d["closing_window_move_pct"]
                    for d in daily_profiles
                    if d.get("closing_window_move_pct") is not None
                ) / closing_valid,
                4,
            ) if closing_valid else None,
            "move_median_pct": round(
                median([
                    d["closing_window_move_pct"]
                    for d in daily_profiles
                    if d.get("closing_window_move_pct") is not None
                ]),
                4,
            ) if closing_valid else None,
        },
        "late_session": {
            "actionable_call_count": len(late_calls),
            "days_with_actionable_call": len({
                x["session_date"] for x in late_calls
            }),
            "spot_session_close": _aggregate_outcomes(
                late_calls, "spot_outcome"
            ),
            "option_1540": _aggregate_outcomes(
                late_calls, "premium_1540_outcome"
            ),
            "calls": late_calls,
        },
    }