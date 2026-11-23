"""Shadow-mode adaptive learning from matured paper outcomes.

This module measures which market regimes/actions/horizons have performed
best using source-aware forward-paper observations. It NEVER changes live
decision weights or creates broker orders. Candidate weights are research
outputs only until separately validated and approved.
"""
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import DailySignalLedger, ForwardPaperOutcome

MIN_SAMPLE_SIZE = 30
MAX_ROWS = 12000
VALID_STATUSES = {"matured", "matured_partial"}


def _regime_from_ledger(row):
    snapshot = row.entry_snapshot if isinstance(row.entry_snapshot, dict) else {}
    capture = snapshot.get("forward_paper_capture")
    if not isinstance(capture, dict):
        return "UNKNOWN"
    regime = capture.get("market_regime")
    if not isinstance(regime, dict):
        return "UNKNOWN"
    value = str(regime.get("regime") or "UNKNOWN").strip().upper()
    return value or "UNKNOWN"


def _measured_return(outcome):
    # Execution-aware return is preferred. Premium adjusted return is a
    # fallback measurement, never confused with an actual filled trade.
    value = outcome.execution_aware_return_pct
    source = "execution_aware"
    if value is None:
        value = outcome.premium_action_adjusted_return_pct
        source = "premium_mid_or_ltp_proxy"
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, None
    if not (-100.0 <= value <= 1000.0):
        return None, None
    return value, source


def build_shadow_report(records, *, minimum_samples=MIN_SAMPLE_SIZE):
    buckets = defaultdict(lambda: {
        "samples": 0, "wins": 0, "losses": 0, "flat": 0,
        "returns": [], "execution_aware_samples": 0,
    })
    excluded = {"unknown_regime": 0, "ungraded": 0, "invalid_return": 0}
    for ledger, outcome in records:
        if outcome.status not in VALID_STATUSES:
            excluded["ungraded"] += 1
            continue
        regime = _regime_from_ledger(ledger)
        if regime == "UNKNOWN":
            excluded["unknown_regime"] += 1
            continue
        value, source = _measured_return(outcome)
        if value is None:
            excluded["invalid_return"] += 1
            continue
        action = str(ledger.action or "UNKNOWN").upper()
        key = (str(ledger.symbol or "").upper(), regime, action, int(outcome.horizon_minutes))
        bucket = buckets[key]
        bucket["samples"] += 1
        bucket["returns"].append(value)
        if source == "execution_aware":
            bucket["execution_aware_samples"] += 1
        if value > 0:
            bucket["wins"] += 1
        elif value < 0:
            bucket["losses"] += 1
        else:
            bucket["flat"] += 1

    results = []
    for (symbol, regime, action, horizon), bucket in sorted(buckets.items()):
        n = bucket["samples"]
        win_rate = bucket["wins"] / n if n else 0.0
        avg_return = sum(bucket["returns"]) / n if n else 0.0
        ready = n >= int(minimum_samples)
        # Bounded candidate weight, for offline comparison only.
        candidate = round(max(0.8, min(1.2, 1.0 + (win_rate - 0.5))), 3) if ready else None
        results.append({
            "symbol": symbol, "market_regime": regime, "action": action,
            "horizon_minutes": horizon, "samples": n,
            "wins": bucket["wins"], "losses": bucket["losses"],
            "flat": bucket["flat"], "win_rate_pct": round(win_rate * 100, 2),
            "average_action_adjusted_return_pct": round(avg_return, 4),
            "execution_aware_samples": bucket["execution_aware_samples"],
            "candidate_weight": candidate,
            "status": "CANDIDATE_SHADOW_ONLY" if ready else "INSUFFICIENT_SAMPLE",
        })
    return {
        "mode": "shadow_only",
        "live_weights_changed": False,
        "minimum_samples": int(minimum_samples),
        "groups": results,
        "excluded": excluded,
        "warning": "Historical paper outcomes are not guarantees. Candidate weights require out-of-sample validation before any live strategy use.",
    }


async def compute_shadow_learning_report(*, symbol=None, days=60, minimum_samples=MIN_SAMPLE_SIZE):
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, min(int(days), 60)))
    stmt = (
        select(DailySignalLedger, ForwardPaperOutcome)
        .join(ForwardPaperOutcome, ForwardPaperOutcome.ledger_id == DailySignalLedger.id)
        .where(DailySignalLedger.timestamp >= cutoff)
        .where(ForwardPaperOutcome.status.in_(tuple(VALID_STATUSES)))
        .order_by(DailySignalLedger.timestamp.desc())
        .limit(MAX_ROWS)
    )
    if symbol:
        stmt = stmt.where(DailySignalLedger.symbol == str(symbol).upper())
    async with AsyncSessionLocal() as session:
        records = (await session.execute(stmt)).all()
    report = build_shadow_report(records, minimum_samples=minimum_samples)
    report.update({"symbol": str(symbol).upper() if symbol else "ALL", "days": max(1, min(int(days), 60)), "records_reviewed": len(records)})
    return report
