from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, and_, or_

from app.database import AsyncSessionLocal
from app.models import DailySignalLedger, MarketData, OptionData
from app.services.signal_accuracy import (
    ESTIMATED_ROUND_TRIP_COST_PCT,
    HORIZONS_MINUTES,
    SPOT_HORIZON_TOLERANCE_MIN,
    _asof_price,
    _grade,
    _grade_premium,
)
from app.utils.helpers import now_utc_naive

LEDGER_TOLERANCE_MIN = 10


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


def _action(action: str) -> str:
    return {
        "BUY CE": "CALL_BUY",
        "BUY PE": "PUT_BUY",
        "SELL CE": "CALL_SELL",
        "SELL PE": "PUT_SELL",
    }.get(str(action or "").upper().strip(), str(action or "").upper().replace(" ", "_"))


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
                str(r.expiry or "").strip(),
                round(float(r.strike), 6),
                str(r.option_type or "").upper().strip(),
            )
            for r in ledger
            if r.expiry and r.strike is not None and r.option_type
        }

        options = []
        if contracts:
            filters = [
                and_(
                    OptionData.expiry == expiry,
                    OptionData.strike == strike,
                    OptionData.option_type == opt,
                )
                for expiry, strike, opt in contracts
            ]
            options = (
                await session.execute(
                    select(OptionData)
                    .where(OptionData.symbol == symbol)
                    .where(
                        OptionData.timestamp >=
                        cutoff - timedelta(minutes=LEDGER_TOLERANCE_MIN)
                    )
                    .where(or_(*filters))
                    .order_by(OptionData.timestamp.asc())
                )
            ).scalars().all()

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
            str(r.expiry or "").strip(),
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
    matured = pending = spot_available = option_available = pnl_ready = 0
    missing_lot = invalid = 0

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
            "entry_spot": entry_spot,
            "entry_premium": entry_premium,
            "lot_size": lot,
        }

        if target > now:
            pending += 1
            item["status"] = "pending"
            episodes.append(item)
            continue

        matured += 1
        later_spot = _asof_price(
            spot_series, spot_ts, target, SPOT_HORIZON_TOLERANCE_MIN
        )

        key = (
            str(r.expiry or "").strip(),
            round(float(r.strike), 6),
            str(r.option_type or "").upper().strip(),
        )
        series = option_series.get(key, [])
        later_premium = _asof_price(
            series, [x[0] for x in series], target, LEDGER_TOLERANCE_MIN
        ) if series else None

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
            if later_spot is not None or later_premium is not None
            else "no_data"
        )
        episodes.append(item)

    return {
        "symbol": symbol,
        "days": days,
        "horizon_minutes": horizon_minutes,
        "basis": "DailySignalLedger read-only replay; no future snapshot is used.",
        "ledger_rows": len(ledger),
        "matured_rows": matured,
        "pending_rows": pending,
        "invalid_rows": invalid,
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
        "episodes": episodes,
    }
