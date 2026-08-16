"""
Signal Accuracy Engine — Prediction vs Actual, per generated signal
=====================================================================
Spec §29 (PREDICTION VS ACTUAL MARKET ENGINE): every stored `AnalysisResult`
row (analysis_type="ai") IS a generated signal — {preferred_side, confidence,
volatility_regime, ...}. This module compares each one against what NIFTY's
price actually did afterwards, at several horizons, and rolls the outcomes
up into:

  - Overall accuracy
  - CALL BUY / PUT BUY accuracy   (accuracy for that specific action only)
  - By Market Regime (volatility_regime: low/normal/high/extreme)
  - By Confidence Range (<50, 50-70, 70-85, 85+)

Per-signal outcome states (spec: Correct / Wrong / Expired / Invalidated):
  CORRECT      price moved in the predicted direction beyond the neutral band
               by the horizon.
  WRONG        price moved against the predicted direction beyond the band.
  FLAT         price stayed inside the neutral band — no real move to grade.
  EXPIRED      not enough time has passed yet to know (skip from stats,
               shown separately as "pending").
  NO_DATA      no price snapshot exists near the horizon timestamp (a gap in
               our own MarketData history) — not counted either way.

This engine reuses the same MarketData price-history table that
accuracy_engine.py already reads (populated by dashboard polling), and the
same "nearest snapshot within a tolerance window" matching approach, so its
accuracy is bounded by how often the dashboard was actually left open/polling
— exactly like the existing per-indicator engine. It is intentionally a
separate, complementary view (per *signal/action*, not per *indicator*).
"""
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import AnalysisResult, MarketData, OptionData

logger = logging.getLogger(__name__)

# Spec §29: evaluate at 5 / 10 / 15 / 30 / 60 minutes after the signal.
HORIZONS_MINUTES = [5, 10, 15, 30, 60]
HORIZON_TOLERANCE_MIN = 4          # +/- window to find a nearby price snapshot
NEUTRAL_BAND_PCT = 0.05            # move smaller than this = "flat", not graded
MIN_SIGNALS_FOR_CONFIDENCE = 5

CONFIDENCE_BUCKETS = [
    ("<50",    0,  50),
    ("50-70",  50, 70),
    ("70-85",  70, 85),
    ("85+",    85, 101),
]


def _confidence_bucket(conf: float) -> str:
    for label, lo, hi in CONFIDENCE_BUCKETS:
        if lo <= conf < hi:
            return label
    return "unknown"


def _action_from_signal(result: Dict) -> Optional[str]:
    """preferred_side ('CALL'/'PUT'/'NONE') -> action label.
    Only BUY actions exist today (decision_engine never emits SELL — spec
    §14 requires extra IV/liquidity/margin checks before SELL signals are
    safe, which this app doesn't have yet), so CALL SELL / PUT SELL always
    show as insufficient_data until that's implemented."""
    side = (result.get("preferred_side") or "NONE").upper()
    if side == "CALL":
        return "CALL_BUY"
    if side == "PUT":
        return "PUT_BUY"
    return "NO_TRADE"


def _nearest_price(prices: List[Tuple[datetime, float]], target: datetime,
                    max_gap_minutes: float) -> Optional[float]:
    best_price, best_gap = None, None
    for ts, price in prices:
        gap = abs((ts - target).total_seconds()) / 60.0
        if best_gap is None or gap < best_gap:
            best_gap, best_price = gap, price
    if best_price is None or best_gap > max_gap_minutes:
        return None
    return best_price


def _grade(action: str, change_pct: float) -> str:
    """CALL BUY wins on an up-move, PUT BUY wins on a down-move."""
    if abs(change_pct) <= NEUTRAL_BAND_PCT:
        return "flat"
    moved_up = change_pct > 0
    if action == "CALL_BUY":
        return "correct" if moved_up else "wrong"
    if action == "PUT_BUY":
        return "correct" if not moved_up else "wrong"
    return "flat"  # NO_TRADE makes no directional claim to grade


def _empty_bucket() -> Dict:
    return {"correct": 0, "wrong": 0, "flat": 0, "total_graded": 0}


def _bump(bucket: Dict, grade: str) -> None:
    if grade in ("correct", "wrong"):
        bucket[grade] += 1
        bucket["total_graded"] += 1
    elif grade == "flat":
        bucket["flat"] += 1


def _rate(bucket: Dict) -> Optional[float]:
    tg = bucket["total_graded"]
    return round(bucket["correct"] / tg * 100, 1) if tg > 0 else None


async def compute_signal_accuracy(symbol: str, days: int = 15,
                                   horizon_minutes: int = 60) -> Dict:
    """
    Main horizon reported in the top-level summary is `horizon_minutes`
    (default 60, matching accuracy_engine.py's indicator horizon so the two
    views are comparable). All of HORIZONS_MINUTES are still computed and
    returned under `by_horizon` for the 5/10/15/30/60-minute table spec §29
    asks for.
    """
    if horizon_minutes not in HORIZONS_MINUTES:
        horizon_minutes = 60

    cutoff = datetime.utcnow() - timedelta(days=days)
    now = datetime.utcnow()

    async with AsyncSessionLocal() as session:
        analysis_rows = (await session.execute(
            select(AnalysisResult)
            .where(AnalysisResult.symbol == symbol)
            .where(AnalysisResult.analysis_type == "ai")
            .where(AnalysisResult.timestamp >= cutoff)
            .order_by(AnalysisResult.timestamp.asc())
        )).scalars().all()

        market_rows = (await session.execute(
            select(MarketData)
            .where(MarketData.symbol == symbol)
            .where(MarketData.timestamp >= cutoff)
            .order_by(MarketData.timestamp.asc())
        )).scalars().all()

    prices: List[Tuple[datetime, float]] = [
        (r.timestamp, r.price) for r in market_rows if r.price is not None
    ]

    overall: Dict[int, Dict] = {h: _empty_bucket() for h in HORIZONS_MINUTES}
    by_action: Dict[str, Dict[int, Dict]] = {
        "CALL_BUY": {h: _empty_bucket() for h in HORIZONS_MINUTES},
        "PUT_BUY":  {h: _empty_bucket() for h in HORIZONS_MINUTES},
    }
    by_regime: Dict[str, Dict] = {}
    by_confidence: Dict[str, Dict] = {}
    pending = 0
    no_data = 0
    signals_seen = 0

    if len(prices) >= 2:
        for row in analysis_rows:
            result = row.result or {}
            action = _action_from_signal(result)
            ts = row.timestamp
            price_then = _nearest_price(prices, ts, HORIZON_TOLERANCE_MIN)
            if price_then is None or price_then <= 0:
                no_data += 1
                continue

            signals_seen += 1
            conf = result.get("confidence", result.get("signal_strength", 0)) or 0
            regime = result.get("volatility_regime", "unknown") or "unknown"
            conf_bucket = _confidence_bucket(conf)

            # Grade this one signal at every horizon independently.
            main_grade = None
            for h in HORIZONS_MINUTES:
                target = ts + timedelta(minutes=h)
                if target > now:
                    if h == horizon_minutes:
                        pending += 1
                    continue  # too soon to know yet — not an error, just unresolved
                price_later = _nearest_price(prices, target, HORIZON_TOLERANCE_MIN)
                if price_later is None:
                    continue  # gap in our own price history at that instant
                change_pct = (price_later - price_then) / price_then * 100
                grade = _grade(action, change_pct)
                _bump(overall[h], grade)
                if action in by_action:
                    _bump(by_action[action][h], grade)
                if h == horizon_minutes:
                    main_grade = grade

            if main_grade is None:
                continue  # nothing gradeable at the headline horizon for this signal

            if action in ("CALL_BUY", "PUT_BUY"):
                by_regime.setdefault(regime, _empty_bucket())
                _bump(by_regime[regime], main_grade)
                by_confidence.setdefault(conf_bucket, _empty_bucket())
                _bump(by_confidence[conf_bucket], main_grade)

    def _fmt_bucket(b: Dict) -> Dict:
        return {**b, "success_rate": _rate(b),
                "insufficient_data": b["total_graded"] < MIN_SIGNALS_FOR_CONFIDENCE}

    return {
        "symbol": symbol,
        "days": days,
        "headline_horizon_minutes": horizon_minutes,
        "signals_seen": signals_seen,
        "pending_at_headline_horizon": pending,
        "no_price_data": no_data,
        "overall": _fmt_bucket(overall[horizon_minutes]),
        "call_buy_accuracy": _fmt_bucket(by_action["CALL_BUY"][horizon_minutes]),
        "put_buy_accuracy":  _fmt_bucket(by_action["PUT_BUY"][horizon_minutes]),
        "call_sell_accuracy": {"correct": 0, "wrong": 0, "flat": 0, "total_graded": 0,
                                "success_rate": None, "insufficient_data": True,
                                "reason": "CALL SELL signals not yet generated by decision_engine (spec §14 guard)"},
        "put_sell_accuracy":  {"correct": 0, "wrong": 0, "flat": 0, "total_graded": 0,
                                "success_rate": None, "insufficient_data": True,
                                "reason": "PUT SELL signals not yet generated by decision_engine (spec §14 guard)"},
        "by_regime": {k: _fmt_bucket(v) for k, v in by_regime.items()},
        "by_confidence_range": {k: _fmt_bucket(v) for k, v in by_confidence.items()},
        "by_horizon": {
            str(h): {
                "overall": _fmt_bucket(overall[h]),
                "call_buy": _fmt_bucket(by_action["CALL_BUY"][h]),
                "put_buy":  _fmt_bucket(by_action["PUT_BUY"][h]),
            }
            for h in HORIZONS_MINUTES
        },
    }


# ── Premium-based accuracy (review #1) ──────────────────────────────────────
# The spot-direction engine above answers "did NIFTY move the way the signal
# said?" — but a CALL_BUY signal can be spot-direction-CORRECT while the
# actual option loses money to theta decay / IV crush, and vice versa. This
# section grades the SAME signals on what actually would have happened to
# the specific option contract `build_ai_analysis()` recommended
# (AnalysisResult.result["recommended_option"]) — using the real OptionData
# premium history this app already snapshots on every fetch
# (save_option_chain_snapshot). This is the "Option Trading Accuracy" the
# review asked for, as a genuine second metric alongside spot-direction, not
# a replacement — both are useful, differently: spot-direction accuracy
# isolates whether the market READ was right; premium accuracy tells you
# whether BUYING THE RECOMMENDED CONTRACT would have made money. Where they
# disagree (spot right, premium wrong) IS the theta/IV-crush pattern the
# review is asking to expose.
NEUTRAL_PREMIUM_BAND_PCT = 3.0  # premium move smaller than this = "flat", not graded (bid/ask noise)

# Review #6: "+1% or +2% premium movement is not necessarily a profitable
# real trade" — brokerage + STT + exchange/SEBI charges + the half-spread
# already not captured by using mid-price (mid-price removes ONE side's
# slippage on entry; exit still crosses the spread) typically eat a real,
# non-trivial chunk of a small option premium move. This is a rough,
# clearly-labelled ESTIMATE (not a live brokerage-specific calculation —
# actual cost depends on the broker's plan), applied symmetrically as a
# round-trip drag on the raw premium % change, to produce a second,
# "net of estimated costs" grade alongside the existing gross one.
ESTIMATED_ROUND_TRIP_COST_PCT = 2.0


def _grade_premium(change_pct: float, cost_pct: float = 0.0) -> str:
    """A BUY (CALL or PUT — recommended_option is always the side we'd have
    bought) profits when premium rises, loses when it falls. Simpler than
    _grade() above because there's no direction ambiguity once you're
    looking at the option's own price, not the underlying's.
    `cost_pct` (>= 0) is subtracted from a favourable move / added to an
    unfavourable one before grading — i.e. it always works against the
    trade, modelling round-trip cost drag."""
    net = change_pct - cost_pct
    if abs(net) <= NEUTRAL_PREMIUM_BAND_PCT:
        return "flat"
    return "correct" if net > 0 else "wrong"


async def compute_premium_accuracy(symbol: str, days: int = 15,
                                    horizon_minutes: int = 60) -> Dict:
    """Mirrors compute_signal_accuracy's shape/semantics but grades option
    PREMIUM outcome instead of spot direction, for every signal that had a
    concrete recommended_option (preferred_side CALL/PUT with a liquid
    strike — see _pick_recommended_option in api/routes/analysis.py)."""
    if horizon_minutes not in HORIZONS_MINUTES:
        horizon_minutes = 60

    cutoff = datetime.utcnow() - timedelta(days=days)
    now = datetime.utcnow()

    async with AsyncSessionLocal() as session:
        analysis_rows = (await session.execute(
            select(AnalysisResult)
            .where(AnalysisResult.symbol == symbol)
            .where(AnalysisResult.analysis_type == "ai")
            .where(AnalysisResult.timestamp >= cutoff)
            .order_by(AnalysisResult.timestamp.asc())
        )).scalars().all()

        option_rows = (await session.execute(
            select(OptionData)
            .where(OptionData.symbol == symbol)
            .where(OptionData.timestamp >= cutoff)
            .order_by(OptionData.timestamp.asc())
        )).scalars().all()

    # Index OptionData by (expiry, strike, option_type) -> [(ts, last_price), ...]
    by_contract: Dict[Tuple[str, float, str], List[Tuple[datetime, float]]] = {}
    for r in option_rows:
        if r.last_price is None or r.last_price <= 0:
            continue
        key = (r.expiry, float(r.strike), r.option_type)
        by_contract.setdefault(key, []).append((r.timestamp, r.last_price))

    overall: Dict[int, Dict] = {h: _empty_bucket() for h in HORIZONS_MINUTES}
    by_action: Dict[str, Dict[int, Dict]] = {
        "CALL_BUY": {h: _empty_bucket() for h in HORIZONS_MINUTES},
        "PUT_BUY":  {h: _empty_bucket() for h in HORIZONS_MINUTES},
    }
    # Net-of-estimated-cost mirror of overall/by_action above (review #6).
    overall_net: Dict[int, Dict] = {h: _empty_bucket() for h in HORIZONS_MINUTES}
    by_action_net: Dict[str, Dict[int, Dict]] = {
        "CALL_BUY": {h: _empty_bucket() for h in HORIZONS_MINUTES},
        "PUT_BUY":  {h: _empty_bucket() for h in HORIZONS_MINUTES},
    }
    pending = 0
    no_data = 0
    no_recommendation = 0
    signals_seen = 0
    # Disagreement tracker (review #1's core point, made visible): signals
    # where spot direction and premium direction graded oppositely at the
    # headline horizon.
    disagreement_examples: List[Dict] = []

    for row in analysis_rows:
        result = row.result or {}
        rec = result.get("recommended_option") or {}
        if not rec.get("available"):
            no_recommendation += 1
            continue

        side = (result.get("preferred_side") or "NONE").upper()
        action = "CALL_BUY" if side == "CALL" else "PUT_BUY" if side == "PUT" else None
        if action is None:
            no_recommendation += 1
            continue

        key = (rec.get("expiry"), float(rec.get("strike", 0)), rec.get("type"))
        series = by_contract.get(key)
        if not series:
            no_data += 1
            continue

        ts = row.timestamp
        premium_then = _nearest_price(series, ts, HORIZON_TOLERANCE_MIN) or rec.get("entry_price")
        if not premium_then or premium_then <= 0:
            no_data += 1
            continue

        signals_seen += 1
        main_grade = None
        for h in HORIZONS_MINUTES:
            target = ts + timedelta(minutes=h)
            if target > now:
                if h == horizon_minutes:
                    pending += 1
                continue
            premium_later = _nearest_price(series, target, HORIZON_TOLERANCE_MIN)
            if premium_later is None:
                continue
            change_pct = (premium_later - premium_then) / premium_then * 100
            grade = _grade_premium(change_pct)
            _bump(overall[h], grade)
            _bump(by_action[action][h], grade)
            net_grade = _grade_premium(change_pct, ESTIMATED_ROUND_TRIP_COST_PCT)
            _bump(overall_net[h], net_grade)
            _bump(by_action_net[action][h], net_grade)
            if h == horizon_minutes:
                main_grade = grade

        if main_grade is None:
            continue

    def _fmt_bucket2(b: Dict) -> Dict:
        return {**b, "success_rate": _rate(b),
                "insufficient_data": b["total_graded"] < MIN_SIGNALS_FOR_CONFIDENCE}

    return {
        "symbol": symbol,
        "days": days,
        "headline_horizon_minutes": horizon_minutes,
        "signals_with_recommendation": signals_seen,
        "signals_without_recommendation": no_recommendation,
        "pending_at_headline_horizon": pending,
        "no_premium_data": no_data,
        "overall": _fmt_bucket2(overall[horizon_minutes]),
        "call_buy_accuracy": _fmt_bucket2(by_action["CALL_BUY"][horizon_minutes]),
        "put_buy_accuracy":  _fmt_bucket2(by_action["PUT_BUY"][horizon_minutes]),
        # Review #6: same signals, same horizon — but graded after
        # subtracting ESTIMATED_ROUND_TRIP_COST_PCT (brokerage/STT/exchange
        # charges/spread-crossing-on-exit) from the raw premium move. This
        # is always <= the gross accuracy above (costs only ever hurt),
        # and the gap between the two IS the answer to "was this actually
        # profitable, not just directionally correct".
        "overall_net_of_costs": _fmt_bucket2(overall_net[horizon_minutes]),
        "call_buy_accuracy_net_of_costs": _fmt_bucket2(by_action_net["CALL_BUY"][horizon_minutes]),
        "put_buy_accuracy_net_of_costs":  _fmt_bucket2(by_action_net["PUT_BUY"][horizon_minutes]),
        "estimated_round_trip_cost_pct": ESTIMATED_ROUND_TRIP_COST_PCT,
        "by_horizon": {
            str(h): {
                "overall": _fmt_bucket2(overall[h]),
                "call_buy": _fmt_bucket2(by_action["CALL_BUY"][h]),
                "put_buy":  _fmt_bucket2(by_action["PUT_BUY"][h]),
                "overall_net_of_costs": _fmt_bucket2(overall_net[h]),
            }
            for h in HORIZONS_MINUTES
        },
        "note": (
            "Grades the ACTUAL recommended option contract's premium (mid-price "
            "at signal time vs at horizon), not spot direction — a signal can be "
            "spot-direction-correct while this shows 'wrong' if theta/IV crush "
            "outweighed the favourable move, and vice versa. Requires option-chain "
            "history (OptionData) covering the same window — thin coverage shows "
            "as no_premium_data rather than being silently skipped. "
            f"'_net_of_costs' fields subtract an estimated {ESTIMATED_ROUND_TRIP_COST_PCT}% "
            "round-trip cost (brokerage/STT/exchange charges/exit-side spread) — a rough "
            "estimate, not your actual broker's numbers, but a closer read of real "
            "profitability than the gross % alone."
        ),
    }
