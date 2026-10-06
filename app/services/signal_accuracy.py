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
from bisect import bisect_left, bisect_right

from sqlalchemy import select, text

from app.database import AsyncSessionLocal
from app.models import AnalysisResult, MarketData, OptionData, IntradayOHLC
from app.services.accuracy_time import analysis_event_timestamp
from app.utils.helpers import now_utc_naive

logger = logging.getLogger(__name__)

# Spec §29: evaluate at 5 / 10 / 15 / 30 / 60 minutes after the signal.
HORIZONS_MINUTES = [5, 10, 15, 30, 60]
SPOT_HORIZON_TOLERANCE_MIN = 10    # max age of historical spot snapshot
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


# ── Confidence calibration ────────────────────────────────────────────────────
# IMPORTANT:
# Calibration is NOT based on every historical preferred_side row.
# It uses only independent ACTIONABLE episodes:
#   - preferred_side = CALL / PUT
#   - signal_lifecycle starts with CONFIRMED_ or HOLD_
#   - signal_active_side matches preferred_side
#   - repeated same-side actionable rows within 10 minutes = one episode
#
# This prevents legacy WATCH rows and repeated HOLD snapshots from inflating
# the calibration sample size.

CALIBRATION_MIN_SIGNALS = MIN_SIGNALS_FOR_CONFIDENCE
CALIBRATION_EPISODE_GAP_MIN = 10

CALIBRATION_CONFIDENCE_BUCKETS = [
    ("30-39", 30, 40),
    ("40-49", 40, 50),
    ("50-59", 50, 60),
    ("60-69", 60, 70),
    ("70-79", 70, 80),
    ("80-89", 80, 90),
    ("90-95", 90, 96),
]

CALIBRATION_DISCLAIMER = (
    "Signal Strength ஒரு statistically-calibrated win probability இல்லை — "
    "இது rule-engine indicators எவ்வளவு agree ஆகின்றன என்பதை அளக்கும் score "
    "மட்டும். Historical Win Rate என்பது independent actionable episodes-ல் "
    "NIFTY உண்மையில் அந்த direction-ல் நகர்ந்ததன் historical அளவு. "
    "ஒவ்வொரு confidence bucket-லும் குறைந்தது 5 graded actionable episodes "
    "இல்லையெனில் அந்த rate நம்பகமான calibration ஆக கருதப்படாது."
)


def _calibration_bucket(conf: float) -> str:
    try:
        value = float(conf)
    except (TypeError, ValueError):
        return "unknown"

    for label, lo, hi in CALIBRATION_CONFIDENCE_BUCKETS:
        if lo <= value < hi:
            return label

    return "unknown"


def _is_actionable_calibration_row(result: Dict) -> bool:
    side = str(result.get("preferred_side") or "NONE").upper()
    lifecycle = str(result.get("signal_lifecycle") or "").upper()
    active_side = str(result.get("signal_active_side") or "NONE").upper()

    return (
        side in ("CALL", "PUT")
        and lifecycle.startswith(("CONFIRMED_", "HOLD_"))
        and active_side == side
    )


def _fmt_calibration_bucket(
    bucket: Dict,
) -> Dict:
    graded = int(bucket.get("graded", 0))
    correct = int(bucket.get("correct", 0))

    sufficient = graded >= CALIBRATION_MIN_SIGNALS

    return {
        "episodes": int(bucket.get("episodes", 0)),
        "correct": correct,
        "wrong": int(bucket.get("wrong", 0)),
        "flat": int(bucket.get("flat", 0)),
        "missing": int(bucket.get("missing", 0)),
        "graded": graded,
        # Do not expose a misleading rate when the bucket is too small.
        "win_rate_pct": (
            round(correct / graded * 100, 1)
            if sufficient and graded > 0
            else None
        ),
        "insufficient_data": not sufficient,
    }


async def calibrate_confidence(
    symbol: str,
    current_confidence: float,
    days: int = 30,
    horizon_minutes: int = 60,
) -> Dict:
    """
    Historical confidence calibration based ONLY on independent actionable
    episodes, not every persisted directional snapshot.

    Episode definition:
      * preferred_side is CALL/PUT
      * lifecycle is CONFIRMED_* or HOLD_*
      * signal_active_side matches preferred_side
      * same-side actionable rows <= 10 minutes apart belong to one episode
      * only the first row of each episode is graded

    A confidence bucket exposes Historical Win Rate only when its own
    graded actionable-episode count reaches CALIBRATION_MIN_SIGNALS.
    """
    bucket = _calibration_bucket(current_confidence)

    if horizon_minutes not in HORIZONS_MINUTES:
        horizon_minutes = 60

    cutoff = now_utc_naive() - timedelta(days=days)
    now = now_utc_naive()

    async with AsyncSessionLocal() as session:
        analysis_rows = (
            await session.execute(
                select(AnalysisResult)
                .where(AnalysisResult.symbol == symbol)
                .where(AnalysisResult.analysis_type == "ai")
                .where(AnalysisResult.timestamp >= cutoff)
                .order_by(AnalysisResult.timestamp.asc())
            )
        ).scalars().all()

        market_rows = (
            await session.execute(
                select(MarketData)
                .where(MarketData.symbol == symbol)
                .where(MarketData.timestamp >= cutoff)
                .order_by(MarketData.timestamp.asc())
            )
        ).scalars().all()


    prices: List[Tuple[datetime, float]] = [
        (r.timestamp, float(r.price))
        for r in market_rows
        if r.price is not None and r.price > 0
    ]
    price_timestamps = [ts for ts, _ in prices]

    # ------------------------------------------------------------
    # Build independent actionable episodes.
    # ------------------------------------------------------------
    episodes = []

    last_side = None
    last_actionable_ts = None

    for row in analysis_rows:
        result = row.result or {}

        if not _is_actionable_calibration_row(result):
            continue

        side = str(result.get("preferred_side") or "NONE").upper()
        ts = analysis_event_timestamp(row)
        if ts is None:
            continue


        is_new_episode = (
            last_side != side
            or last_actionable_ts is None
            or (ts - last_actionable_ts).total_seconds()
               > CALIBRATION_EPISODE_GAP_MIN * 60
        )

        if is_new_episode:
            episodes.append(row)

        last_side = side
        last_actionable_ts = ts

    # ------------------------------------------------------------
    # Grade the FIRST row of each independent actionable episode.
    # ------------------------------------------------------------
    buckets: Dict[str, Dict] = {
        label: {
            "episodes": 0,
            "correct": 0,
            "wrong": 0,
            "flat": 0,
            "missing": 0,
            "graded": 0,
        }
        for label, _, _ in CALIBRATION_CONFIDENCE_BUCKETS
    }

    pending = 0
    unknown_bucket = 0

    for row in episodes:
        result = row.result or {}

        side = str(result.get("preferred_side") or "NONE").upper()
        conf = result.get("confidence", result.get("signal_strength", 0)) or 0
        conf_bucket = _calibration_bucket(conf)

        if conf_bucket not in buckets:
            unknown_bucket += 1
            continue

        bucket_data = buckets[conf_bucket]
        bucket_data["episodes"] += 1

        ts = analysis_event_timestamp(row)
        if ts is None:
            continue

        price_then = _asof_price(
            prices,
            price_timestamps,
            ts,
            SPOT_HORIZON_TOLERANCE_MIN,
        )

        if price_then is None or price_then <= 0:
            bucket_data["missing"] += 1
            continue

        target = ts + timedelta(minutes=horizon_minutes)

        if target > now:
            pending += 1
            continue

        price_later = _asof_price(
            prices,
            price_timestamps,
            target,
            SPOT_HORIZON_TOLERANCE_MIN,
        )

        if price_later is None or price_later <= 0:
            bucket_data["missing"] += 1
            continue

        change_pct = (price_later - price_then) / price_then * 100.0
        action = "CALL_BUY" if side == "CALL" else "PUT_BUY"
        grade = _grade(action, change_pct)

        if grade == "correct":
            bucket_data["correct"] += 1
            bucket_data["graded"] += 1
        elif grade == "wrong":
            bucket_data["wrong"] += 1
            bucket_data["graded"] += 1
        elif grade == "flat":
            bucket_data["flat"] += 1

    curve = []

    for label, _, _ in CALIBRATION_CONFIDENCE_BUCKETS:
        b = _fmt_calibration_bucket(buckets[label])
        curve.append({
            "range": label,
            "win_rate_pct": b["win_rate_pct"],
            "sample_size": b["graded"],
            "episodes": b["episodes"],
            "graded": b["graded"],
            "insufficient_data": b["insufficient_data"],
        })

    matched_raw = buckets.get(bucket)
    matched = _fmt_calibration_bucket(matched_raw) if matched_raw else {
        "episodes": 0,
        "correct": 0,
        "wrong": 0,
        "flat": 0,
        "missing": 0,
        "graded": 0,
        "win_rate_pct": None,
        "insufficient_data": True,
    }

    return {
        "symbol": symbol,
        "signal_strength": current_confidence,
        "confidence_bucket": bucket,
        "historical_win_rate_pct": matched["win_rate_pct"],
        "sample_size": matched["graded"],
        "episodes": matched["episodes"],
        "graded": matched["graded"],
        "insufficient_data": matched["insufficient_data"],
        "min_signals_required": CALIBRATION_MIN_SIGNALS,
        "lookback_days": days,
        "horizon_minutes": horizon_minutes,
        "episode_gap_minutes": CALIBRATION_EPISODE_GAP_MIN,
        "actionable_episodes_total": len(episodes),
        "pending_episodes": pending,
        "unknown_confidence_bucket_episodes": unknown_bucket,
        "calibration_basis": "ACTIONABLE_INDEPENDENT_EPISODES",
        "calibration_curve": curve,
        "disclaimer": CALIBRATION_DISCLAIMER,
    }


# EXPLICIT_ACTION_ACCURACY_FIX_20260922
EXPLICIT_TRADE_ACTIONS = frozenset({
    "CALL_BUY",
    "PUT_BUY",
    "CALL_SELL",
    "PUT_SELL",
})


def _explicit_action_from_signal(result: Dict) -> Optional[str]:
    """Resolve only an explicit final trade action.

    WAIT/None is intentionally not converted into CALL_BUY/PUT_BUY.
    """
    raw = str(
        result.get("signal_action")
        or result.get("best_strategy")
        or ""
    ).strip().upper()

    mapping = {
        "BUY CE": "CALL_BUY",
        "BUY PE": "PUT_BUY",
        "SELL CE": "CALL_SELL",
        "SELL PE": "PUT_SELL",
    }

    return mapping.get(raw)


def _action_from_signal(result: Dict) -> Optional[str]:
    """Resolve the machine action used for accuracy grading.

    Prefer the final action persisted by Strategy/Analysis.  Fall back to
    preferred_side so older historical rows remain gradeable.
    """
    raw = str(
        result.get("signal_action")
        or result.get("best_strategy")
        or ""
    ).strip().upper()

    mapping = {
        "BUY CE": "CALL_BUY",
        "BUY PE": "PUT_BUY",
        "SELL CE": "CALL_SELL",
        "SELL PE": "PUT_SELL",
    }
    if raw in mapping:
        return mapping[raw]

    side = str(result.get("preferred_side") or "NONE").upper()
    if side == "CALL":
        return "CALL_BUY"
    if side == "PUT":
        return "PUT_BUY"
    return "NO_TRADE"


def _nearest_price(
    prices: List[Tuple[datetime, float]],
    timestamps: List[datetime],
    target: datetime,
    max_gap_minutes: float,
) -> Optional[float]:
    """Return the price nearest to target using binary search on sorted timestamps."""
    if not prices:
        return None

    i = bisect_left(timestamps, target)

    if i == 0:
        best_ts, best_price = prices[0]
    elif i == len(prices):
        best_ts, best_price = prices[-1]
    else:
        left = prices[i - 1]
        right = prices[i]

        left_gap = abs((left[0] - target).total_seconds())
        right_gap = abs((right[0] - target).total_seconds())

        # Exact tie: preserve old linear-scan behavior by choosing left.
        best_ts, best_price = (
            left if left_gap <= right_gap else right
        )

    if abs((best_ts - target).total_seconds()) / 60.0 > max_gap_minutes:
        return None

    return best_price


def _asof_price(
    prices: List[Tuple[datetime, float]],
    timestamps: List[datetime],
    target: datetime,
    max_gap_minutes: float,
) -> Optional[float]:
    """Return the latest price at or before target, never a future snapshot."""
    if not prices:
        return None

    i = bisect_left(timestamps, target)

    # Exact snapshot is valid and must be preferred.
    if i < len(prices) and timestamps[i] == target:
        return prices[i][1]

    if i == 0:
        return None

    best_ts, best_price = prices[i - 1]

    if (target - best_ts).total_seconds() / 60.0 > max_gap_minutes:
        return None

    return best_price



def _asof_completed_ohlc_price(
    rows,
    target: datetime,
    max_gap_minutes: float,
) -> Optional[float]:
    # IntradayOHLC.timestamp is the candle START time.
    # The candle becomes observable only at timestamp + 5 minutes.
    best_price = None
    best_effective_ts = None

    for row in rows:
        ts = getattr(row, "timestamp", None)
        close = getattr(row, "close", None)
        if ts is None or close is None:
            continue
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        effective_ts = ts + timedelta(minutes=5)
        if effective_ts > target:
            break
        best_price = close
        best_effective_ts = effective_ts

    if best_price is None or best_effective_ts is None:
        return None

    gap_minutes = (
        target - best_effective_ts
    ).total_seconds() / 60.0
    if gap_minutes > max_gap_minutes:
        return None
    return best_price

def _grade(action: str, change_pct: float) -> str:
    """Grade the underlying-direction expectation for all four actions.

    CALL_BUY  -> bullish: correct when underlying moves up.
    PUT_BUY   -> bearish: correct when underlying moves down.
    CALL_SELL -> bearish: correct when underlying moves down.
    PUT_SELL  -> bullish: correct when underlying moves up.

    NO_TRADE is kept separate from flat. A flat result means a directional
    signal was made but the underlying did not move enough. NO_TRADE means
    the engine deliberately made no directional recommendation.
    """
    directional_actions = (
        "CALL_BUY",
        "PUT_BUY",
        "CALL_SELL",
        "PUT_SELL",
    )

    if action not in directional_actions:
        return "no_trade"

    if abs(change_pct) <= NEUTRAL_BAND_PCT:
        return "flat"

    moved_up = change_pct > 0

    bullish_actions = ("CALL_BUY", "PUT_SELL")

    if action in bullish_actions:
        return "correct" if moved_up else "wrong"

    return "correct" if not moved_up else "wrong"


def _empty_bucket() -> Dict:
    return {
        "correct": 0,
        "wrong": 0,
        "flat": 0,
        "no_trade": 0,
        "total_graded": 0,
    }


def _bump(bucket: Dict, grade: str) -> None:
    if grade in ("correct", "wrong"):
        bucket[grade] += 1
        bucket["total_graded"] += 1
    elif grade in ("flat", "no_trade"):
        bucket[grade] += 1


def _rate(bucket: Dict) -> Optional[float]:
    tg = bucket["total_graded"]
    return round(bucket["correct"] / tg * 100, 1) if tg > 0 else None


async def compute_signal_accuracy(
    symbol: str,
    days: int = 15,
    horizon_minutes: int = 60,
) -> Dict:
    """
    Spot-direction accuracy for INDEPENDENT ACTIONABLE episodes.

    An actionable episode requires:
      - preferred_side = CALL / PUT
      - signal_lifecycle starts with CONFIRMED_ or HOLD_
      - signal_active_side matches preferred_side

    Repeated same-side actionable rows within 10 minutes are one episode.
    Only the first row of each episode is graded.

    This prevents repeated WATCH/HOLD snapshots from inflating accuracy.
    Option-premium accuracy remains a separate metric.
    """
    if horizon_minutes not in HORIZONS_MINUTES:
        horizon_minutes = 60

    cutoff = now_utc_naive() - timedelta(days=days)
    now = now_utc_naive()

    async with AsyncSessionLocal() as session:
        analysis_rows = (
            await session.execute(
                select(AnalysisResult)
                .where(AnalysisResult.symbol == symbol)
                .where(AnalysisResult.analysis_type == "ai")
                .where(AnalysisResult.timestamp >= cutoff)
                .order_by(AnalysisResult.timestamp.asc())
            )
        ).scalars().all()

        market_rows = (
            await session.execute(
                select(MarketData)
                .where(MarketData.symbol == symbol)
                .where(MarketData.timestamp >= cutoff)
                .order_by(MarketData.timestamp.asc())
            )
        ).scalars().all()

        # Historical fallback: persisted real completed 5-minute
        # candles. Timestamp is candle-start time; close is observable
        # only at timestamp + 5 minutes.
        intraday_rows = (
            await session.execute(
                select(IntradayOHLC)
                .where(IntradayOHLC.symbol == symbol)
                .where(
                    IntradayOHLC.timestamp >= (
                        cutoff - timedelta(minutes=5)
                    )
                )
                .order_by(IntradayOHLC.timestamp.asc())
            )
        ).scalars().all()

    prices: List[Tuple[datetime, float]] = [
        (r.timestamp, float(r.price))
        for r in market_rows
        if r.price is not None and r.price > 0
    ]
    price_timestamps = [ts for ts, _ in prices]

    # ------------------------------------------------------------
    # Build independent actionable episodes.
    # ------------------------------------------------------------
    episodes = []
    last_side = None
    last_action = None
    last_actionable_ts = None

    for row in analysis_rows:
        result = row.result or {}

        if not _is_actionable_calibration_row(result):
            continue

        action = _explicit_action_from_signal(result)
        if action not in EXPLICIT_TRADE_ACTIONS:
            continue

        side = str(result.get("preferred_side") or "NONE").upper()
        ts = analysis_event_timestamp(row)
        if ts is None:
            continue


        new_episode = (
            last_side != side
            or last_action != action
            or last_actionable_ts is None
            or (ts - last_actionable_ts).total_seconds()
               > CALIBRATION_EPISODE_GAP_MIN * 60
        )

        if new_episode:
            episodes.append(row)

        last_side = side
        last_action = action
        last_actionable_ts = ts

    overall = {
        h: _empty_bucket()
        for h in HORIZONS_MINUTES
    }

    by_action = {
        "CALL_BUY": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "PUT_BUY": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "CALL_SELL": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "PUT_SELL": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
    }

    by_regime: Dict[str, Dict] = {}
    by_confidence: Dict[str, Dict] = {}

    pending = 0
    no_data = 0

    # ------------------------------------------------------------
    # Grade each independent episode.
    # ------------------------------------------------------------
    for row in episodes:
        result = row.result or {}

        side = str(result.get("preferred_side") or "NONE").upper()
        action = _explicit_action_from_signal(result)
        if action not in EXPLICIT_TRADE_ACTIONS:
            continue

        conf = result.get(
            "confidence",
            result.get("signal_strength", 0),
        ) or 0

        regime = result.get("volatility_regime", "unknown") or "unknown"
        conf_bucket = _confidence_bucket(conf)

        ts = analysis_event_timestamp(row)
        if ts is None:
            continue

        price_then = _asof_price(
            prices,
            price_timestamps,
            ts,
            SPOT_HORIZON_TOLERANCE_MIN,
        )

        if price_then is None or price_then <= 0:
            price_then = _asof_completed_ohlc_price(
                intraday_rows,
                ts,
                SPOT_HORIZON_TOLERANCE_MIN,
            )

        if price_then is None or price_then <= 0:
            no_data += 1
            continue

        main_grade = None

        for h in HORIZONS_MINUTES:
            target = ts + timedelta(minutes=h)

            if target > now:
                if h == horizon_minutes:
                    pending += 1
                continue

            price_later = _asof_price(
                prices,
                price_timestamps,
                target,
                SPOT_HORIZON_TOLERANCE_MIN,
            )

            if price_later is None or price_later <= 0:
                price_later = _asof_completed_ohlc_price(
                    intraday_rows,
                    target,
                    SPOT_HORIZON_TOLERANCE_MIN,
                )

            if price_later is None or price_later <= 0:
                if h == horizon_minutes:
                    no_data += 1
                continue

            change_pct = (
                (price_later - price_then)
                / price_then
                * 100.0
            )

            grade = _grade(action, change_pct)

            _bump(overall[h], grade)
            _bump(by_action[action][h], grade)

            if h == horizon_minutes:
                main_grade = grade

        if main_grade is None:
            continue

        by_regime.setdefault(regime, _empty_bucket())
        _bump(by_regime[regime], main_grade)

        by_confidence.setdefault(conf_bucket, _empty_bucket())
        _bump(by_confidence[conf_bucket], main_grade)

    def _fmt_bucket(b: Dict) -> Dict:
        return {
            **b,
            "success_rate": _rate(b),
            "insufficient_data": (
                b["total_graded"] < MIN_SIGNALS_FOR_CONFIDENCE
            ),
        }

    return {
        "symbol": symbol,
        "days": days,
        "headline_horizon_minutes": horizon_minutes,

        # Important: this is episodes, not raw persisted signal rows.
        "signals_seen": len(episodes),
        "actionable_episodes": len(episodes),
        "episode_gap_minutes": CALIBRATION_EPISODE_GAP_MIN,

        "pending_at_headline_horizon": pending,
        "no_price_data": no_data,
        "no_directional_recommendation": 0,

        "overall": _fmt_bucket(
            overall[horizon_minutes]
        ),

        "call_buy_accuracy": _fmt_bucket(
            by_action["CALL_BUY"][horizon_minutes]
        ),

        "put_buy_accuracy": _fmt_bucket(
            by_action["PUT_BUY"][horizon_minutes]
        ),

        "call_sell_accuracy": _fmt_bucket(
            by_action["CALL_SELL"][horizon_minutes]
        ),

        "put_sell_accuracy": _fmt_bucket(
            by_action["PUT_SELL"][horizon_minutes]
        ),

        "by_regime": {
            k: _fmt_bucket(v)
            for k, v in by_regime.items()
        },

        "by_confidence_range": {
            k: _fmt_bucket(v)
            for k, v in by_confidence.items()
        },

        "by_horizon": {
            str(h): {
                "overall": _fmt_bucket(overall[h]),
                "call_buy": _fmt_bucket(
                    by_action["CALL_BUY"][h]
                ),
                "put_buy": _fmt_bucket(
                    by_action["PUT_BUY"][h]
                ),
                "call_sell": _fmt_bucket(
                    by_action["CALL_SELL"][h]
                ),
                "put_sell": _fmt_bucket(
                    by_action["PUT_SELL"][h]
                ),
            }
            for h in HORIZONS_MINUTES
        },

        "accuracy_basis": "EXPLICIT_ACTION_INDEPENDENT_EPISODES",
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

# Option snapshots are normally ~5-6 minutes apart.
# Keep horizon grading close to the requested signal-time target
# and reject stale snapshots beyond this tolerance.
PREMIUM_HORIZON_TOLERANCE_MIN = 10

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


async def compute_premium_accuracy(
    symbol: str,
    days: int = 15,
    horizon_minutes: int = 60,
) -> Dict:
    """
    Premium accuracy for the ACTUAL recommended option contract,
    using independent actionable episodes.

    Episode definition is identical to Spot Direction Accuracy:
      - preferred_side = CALL / PUT
      - signal_lifecycle starts with CONFIRMED_ or HOLD_
      - signal_active_side matches preferred_side
      - repeated same-side actionable rows within 10 minutes = one episode
      - only the FIRST actionable row of each episode is graded

    Existing outputs are preserved:
      - gross premium accuracy
      - estimated-cost net accuracy
      - by horizon
      - by moneyness
      - MFE / MAE
    """
    if horizon_minutes not in HORIZONS_MINUTES:
        horizon_minutes = 60

    cutoff = now_utc_naive() - timedelta(days=days)
    now = now_utc_naive()

    # ------------------------------------------------------------
    # Phase 1: Load AI history and build independent actionable
    # episodes.
    # ------------------------------------------------------------
    async with AsyncSessionLocal() as session:
        analysis_rows = (
            await session.execute(
                select(AnalysisResult)
                .where(AnalysisResult.symbol == symbol)
                .where(AnalysisResult.analysis_type == "ai")
                .where(AnalysisResult.timestamp >= cutoff)
                .order_by(AnalysisResult.timestamp.asc())
            )
        ).scalars().all()

        market_rows = (
            await session.execute(
                select(MarketData)
                .where(MarketData.symbol == symbol)
                .where(MarketData.timestamp >= cutoff)
                .order_by(MarketData.timestamp.asc())
            )
        ).scalars().all()

    episodes = []
    last_side = None
    last_action = None
    last_actionable_ts = None

    for row in analysis_rows:
        result = row.result or {}

        if not _is_actionable_calibration_row(result):
            continue

        action = _explicit_action_from_signal(result)
        if action not in EXPLICIT_TRADE_ACTIONS:
            continue

        side = str(
            result.get("preferred_side") or "NONE"
        ).upper()

        ts = analysis_event_timestamp(row)
        if ts is None:
            continue


        is_new_episode = (
            last_side != side
            or last_action != action
            or last_actionable_ts is None
            or (
                (ts - last_actionable_ts).total_seconds()
                > CALIBRATION_EPISODE_GAP_MIN * 60
            )
        )

        if is_new_episode:
            episodes.append(row)

        last_side = side
        last_action = action
        last_actionable_ts = ts

    # ------------------------------------------------------------
    # Build spot series for the episode-level Direction vs Premium
    # comparison. This uses the same as-of / no-future-data rule as
    # compute_signal_accuracy().
    # ------------------------------------------------------------
    spot_prices: List[Tuple[datetime, float]] = [
        (r.timestamp, float(r.price))
        for r in market_rows
        if r.price is not None and r.price > 0
    ]
    spot_timestamps = [ts for ts, _ in spot_prices]

    # ------------------------------------------------------------
    # Phase 2: Extract only contracts used by the episode FIRST rows.
    # ------------------------------------------------------------
    required_contracts = set()

    for row in episodes:
        result = row.result or {}
        rec = result.get("recommended_option") or {}

        if not rec.get("available"):
            continue

        side = str(
            result.get("preferred_side") or "NONE"
        ).upper()

        if side not in ("CALL", "PUT"):
            continue

        expiry = rec.get("expiry")
        option_type = rec.get("type")

        try:
            strike = float(rec.get("strike"))
        except (TypeError, ValueError):
            continue

        if not expiry or not option_type or strike <= 0:
            continue

        required_contracts.add(
            (
                str(expiry),
                strike,
                str(option_type).upper(),
            )
        )

    # ------------------------------------------------------------
    # Phase 3: Query only required contracts.
    # ------------------------------------------------------------
    option_rows = []

    if required_contracts:
        from sqlalchemy import tuple_

        contract_list = list(required_contracts)
        batch_size = 150

        async with AsyncSessionLocal() as session:
            for batch_start in range(
                0,
                len(contract_list),
                batch_size,
            ):
                batch = contract_list[
                    batch_start:batch_start + batch_size
                ]

                rows = (
                    await session.execute(
                        select(
                            OptionData.expiry,
                            OptionData.strike,
                            OptionData.option_type,
                            OptionData.timestamp,
                            OptionData.last_price,
                        )
                        .where(
                            OptionData.symbol == symbol
                        )
                        .where(
                            OptionData.timestamp >= cutoff
                        )
                        .where(
                            tuple_(
                                OptionData.expiry,
                                OptionData.strike,
                                OptionData.option_type,
                            ).in_(batch)
                        )
                        .order_by(
                            OptionData.timestamp.asc()
                        )
                    )
                ).all()

                option_rows.extend(rows)

    # ------------------------------------------------------------
    # Phase 4: Build contract time-series.
    # ------------------------------------------------------------
    by_contract: Dict[
        Tuple[str, float, str],
        List[Tuple[datetime, float]]
    ] = {}

    for r in option_rows:
        if r.last_price is None or r.last_price <= 0:
            continue

        key = (
            str(r.expiry),
            float(r.strike),
            str(r.option_type).upper(),
        )

        by_contract.setdefault(key, []).append(
            (
                r.timestamp,
                float(r.last_price),
            )
        )

    contract_timestamps = {
        key: [ts for ts, _ in series]
        for key, series in by_contract.items()
    }

    # ------------------------------------------------------------
    # Phase 5: Output buckets.
    # ------------------------------------------------------------
    overall = {
        h: _empty_bucket()
        for h in HORIZONS_MINUTES
    }

    by_action = {
        "CALL_BUY": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "PUT_BUY": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "CALL_SELL": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "PUT_SELL": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
    }

    overall_net = {
        h: _empty_bucket()
        for h in HORIZONS_MINUTES
    }

    by_action_net = {
        "CALL_BUY": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "PUT_BUY": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "CALL_SELL": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
        "PUT_SELL": {
            h: _empty_bucket()
            for h in HORIZONS_MINUTES
        },
    }

    pending = 0
    no_data = 0
    no_recommendation = 0
    signals_seen = 0

    by_moneyness = {}

    # EXACT_STRIKE_RELIABILITY_PATCH_20260924
    # Exact recommended contract: expiry + strike + option type.
    # Existing by_moneyness aggregation remains unchanged.
    by_strike = {}

    mfe_mae_samples = {
        "overall": [],
        "CALL_BUY": [],
        "PUT_BUY": [],
        "CALL_SELL": [],
        "PUT_SELL": [],
    }

    disagreement = {
        "both_correct": 0,
        "both_wrong": 0,
        "spot_correct_premium_wrong": 0,
        "spot_wrong_premium_correct": 0,
        "spot_graded_premium_flat": 0,
        "spot_flat_premium_graded": 0,
        "missing": 0,
    }

    # ------------------------------------------------------------
    # Phase 6: Grade FIRST row of each actionable episode.
    # ------------------------------------------------------------
    for row in episodes:
        result = row.result or {}
        rec = result.get("recommended_option") or {}

        if not rec.get("available"):
            no_recommendation += 1
            continue

        action = _explicit_action_from_signal(result)
        if action not in EXPLICIT_TRADE_ACTIONS:
            no_recommendation += 1
            continue

        try:
            strike = float(
                rec.get("strike", 0)
            )
        except (TypeError, ValueError):
            strike = 0.0

        expiry = rec.get("expiry")
        option_type = rec.get("type")

        if (
            not expiry
            or not option_type
            or strike <= 0
        ):
            no_recommendation += 1
            continue

        key = (
            str(expiry),
            strike,
            str(option_type).upper(),
        )

        series = by_contract.get(key)

        if not series:
            no_data += 1
            continue

        timestamps = contract_timestamps[key]

        ts = analysis_event_timestamp(row)
        if ts is None:
            continue


        # --------------------------------------------------------
        # Entry premium:
        # latest real snapshot at or BEFORE signal timestamp.
        # Never use future data or rec["entry_price"].
        # --------------------------------------------------------
        entry_i = bisect_right(
            timestamps,
            ts,
        ) - 1

        if entry_i < 0:
            no_data += 1
            continue

        entry_ts, premium_then = series[entry_i]

        entry_gap_minutes = (
            ts - entry_ts
        ).total_seconds() / 60.0

        if (
            premium_then is None
            or premium_then <= 0
            or entry_gap_minutes
            > PREMIUM_HORIZON_TOLERANCE_MIN
        ):
            no_data += 1
            continue

        moneyness = rec.get("label") or "unknown"

        by_moneyness.setdefault(
            moneyness,
            {
                h: _empty_bucket()
                for h in HORIZONS_MINUTES
            },
        )

        # EXACT_STRIKE_EXPIRY_NORMALIZE_20260924
        # Normalize expiry ONLY for exact-strike aggregation.
        # The real by_contract lookup above remains unchanged.
        normalized_expiry = (
            str(expiry)
            .upper()
            .replace("-", "")
            .replace(" ", "")
        )

        strike_key = (
            normalized_expiry,
            float(strike),
            str(option_type).upper(),
        )

        by_strike.setdefault(
            strike_key,
            {
                h: _empty_bucket()
                for h in HORIZONS_MINUTES
            },
        )

        signals_seen += 1
        main_grade = None

        headline_target = (
            ts + timedelta(minutes=horizon_minutes)
        )

        # --------------------------------------------------------
        # Spot-direction grade for the SAME episode.
        # --------------------------------------------------------
        spot_grade = None

        spot_then = _asof_price(
            spot_prices,
            spot_timestamps,
            ts,
            SPOT_HORIZON_TOLERANCE_MIN,
        )

        if (
            spot_then is not None
            and headline_target <= now
        ):
            spot_later = _asof_price(
                spot_prices,
                spot_timestamps,
                headline_target,
                SPOT_HORIZON_TOLERANCE_MIN,
            )

            if spot_later is not None and spot_later > 0:
                spot_change_pct = (
                    (spot_later - spot_then)
                    / spot_then
                    * 100.0
                )
                spot_grade = _grade(
                    action,
                    spot_change_pct,
                )

        # --------------------------------------------------------
        # Horizon grading.
        # --------------------------------------------------------
        for h in HORIZONS_MINUTES:
            target = ts + timedelta(minutes=h)

            if target > now:
                if h == horizon_minutes:
                    pending += 1
                continue

            premium_later = _asof_price(
                series,
                timestamps,
                target,
                PREMIUM_HORIZON_TOLERANCE_MIN,
            )

            if premium_later is None:
                if h == horizon_minutes:
                    no_data += 1
                continue

            change_pct = (
                (premium_later - premium_then)
                / premium_then
                * 100.0
            )

            # For SELL actions, a falling option premium is favorable.
            # Normalize the premium move so _grade_premium() always
            # evaluates from the action's point of view.
            graded_change_pct = change_pct

            if action in ("CALL_SELL", "PUT_SELL"):
                graded_change_pct = -change_pct

            gross_grade = _grade_premium(
                graded_change_pct
            )

            _bump(
                overall[h],
                gross_grade,
            )

            _bump(
                by_action[action][h],
                gross_grade,
            )

            _bump(
                by_moneyness[moneyness][h],
                gross_grade,
            )

            _bump(
                by_strike[strike_key][h],
                gross_grade,
            )

            net_grade = _grade_premium(
                graded_change_pct,
                ESTIMATED_ROUND_TRIP_COST_PCT,
            )

            _bump(
                overall_net[h],
                net_grade,
            )

            _bump(
                by_action_net[action][h],
                net_grade,
            )

            if h == horizon_minutes:
                main_grade = gross_grade

        # --------------------------------------------------------
        # Direction vs Premium disagreement:
        # compare the SAME episode at the headline horizon.
        # --------------------------------------------------------
        if spot_grade in ("correct", "wrong", "flat") and main_grade in (
            "correct", "wrong", "flat"
        ):
            if spot_grade == "correct" and main_grade == "correct":
                disagreement["both_correct"] += 1
            elif spot_grade == "wrong" and main_grade == "wrong":
                disagreement["both_wrong"] += 1
            elif (
                spot_grade == "correct"
                and main_grade == "wrong"
            ):
                disagreement["spot_correct_premium_wrong"] += 1
            elif (
                spot_grade == "wrong"
                and main_grade == "correct"
            ):
                disagreement["spot_wrong_premium_correct"] += 1
            elif (
                spot_grade in ("correct", "wrong")
                and main_grade == "flat"
            ):
                disagreement["spot_graded_premium_flat"] += 1
            elif (
                spot_grade == "flat"
                and main_grade in ("correct", "wrong")
            ):
                disagreement["spot_flat_premium_graded"] += 1
        else:
            disagreement["missing"] += 1

        # --------------------------------------------------------
        # MFE / MAE measured from SIGNAL time forward.
        # --------------------------------------------------------
        window_end = min(
            headline_target,
            now,
        )

        start_i = bisect_left(
            timestamps,
            ts,
        )

        end_i = bisect_right(
            timestamps,
            window_end,
        )

        if end_i > start_i:
            window_prices = [
                price
                for _, price
                in series[start_i:end_i]
            ]

            if window_prices:
                changes = [
                    (
                        (price - premium_then)
                        / premium_then
                        * 100
                    )
                    for price in window_prices
                ]

                if action in ("CALL_SELL", "PUT_SELL"):
                    changes = [-x for x in changes]

                sample = {
                    "mfe_pct": round(max(changes), 1),
                    "mae_pct": round(min(changes), 1),
                }

                mfe_mae_samples[
                    "overall"
                ].append(sample)

                mfe_mae_samples[
                    action
                ].append(sample)

        if main_grade is None:
            continue

    # ------------------------------------------------------------
    # Formatting helpers.
    # ------------------------------------------------------------
    def _fmt_bucket2(b: Dict) -> Dict:
        return {
            **b,
            "success_rate": _rate(b),
            "insufficient_data": (
                b["total_graded"]
                < MIN_SIGNALS_FOR_CONFIDENCE
            ),
        }

    def _mfe_mae_summary(
        samples: List[Dict],
    ) -> Dict:
        if not samples:
            return {
                "sample_size": 0,
                "insufficient_data": True,
                "avg_mfe_pct": None,
                "avg_mae_pct": None,
                "best_mfe_pct": None,
                "worst_mae_pct": None,
            }

        mfes = [
            s["mfe_pct"]
            for s in samples
        ]

        maes = [
            s["mae_pct"]
            for s in samples
        ]

        return {
            "sample_size": len(samples),
            "insufficient_data": (
                len(samples)
                < MIN_SIGNALS_FOR_CONFIDENCE
            ),
            "avg_mfe_pct": round(
                sum(mfes) / len(mfes),
                1,
            ),
            "avg_mae_pct": round(
                sum(maes) / len(maes),
                1,
            ),
            "best_mfe_pct": round(
                max(mfes),
                1,
            ),
            "worst_mae_pct": round(
                min(maes),
                1,
            ),
        }

    return {
        "symbol": symbol,
        "days": days,
        "headline_horizon_minutes": horizon_minutes,

        # Backward-compatible keys, but now episode semantics.
        "signals_with_recommendation": signals_seen,
        "signals_without_recommendation": no_recommendation,

        "actionable_episodes": len(episodes),
        "signals_seen": len(episodes),
        "episode_gap_minutes": (
            CALIBRATION_EPISODE_GAP_MIN
        ),

        "pending_at_headline_horizon": pending,
        "no_premium_data": no_data,

        "overall": _fmt_bucket2(
            overall[horizon_minutes]
        ),

        "call_buy_accuracy": _fmt_bucket2(
            by_action["CALL_BUY"][
                horizon_minutes
            ]
        ),

        "put_buy_accuracy": _fmt_bucket2(
            by_action["PUT_BUY"][
                horizon_minutes
            ]
        ),

        "call_sell_accuracy": _fmt_bucket2(
            by_action["CALL_SELL"][
                horizon_minutes
            ]
        ),

        "put_sell_accuracy": _fmt_bucket2(
            by_action["PUT_SELL"][
                horizon_minutes
            ]
        ),

        "overall_net_of_costs": _fmt_bucket2(
            overall_net[horizon_minutes]
        ),

        "call_buy_accuracy_net_of_costs":
            _fmt_bucket2(
                by_action_net["CALL_BUY"][
                    horizon_minutes
                ]
            ),

        "put_buy_accuracy_net_of_costs":
            _fmt_bucket2(
                by_action_net["PUT_BUY"][
                    horizon_minutes
                ]
            ),

        "call_sell_accuracy_net_of_costs":
            _fmt_bucket2(
                by_action_net["CALL_SELL"][
                    horizon_minutes
                ]
            ),

        "put_sell_accuracy_net_of_costs":
            _fmt_bucket2(
                by_action_net["PUT_SELL"][
                    horizon_minutes
                ]
            ),

        "estimated_round_trip_cost_pct":
            ESTIMATED_ROUND_TRIP_COST_PCT,

        "disagreement": {
            **disagreement,
            "comparable": (
                disagreement["both_correct"]
                + disagreement["both_wrong"]
                + disagreement["spot_correct_premium_wrong"]
                + disagreement["spot_wrong_premium_correct"]
                + disagreement["spot_graded_premium_flat"]
                + disagreement["spot_flat_premium_graded"]
            ),
            "total_episodes": len(episodes),
            "direct_direction_premium_disagreements": (
                disagreement["spot_correct_premium_wrong"]
                + disagreement["spot_wrong_premium_correct"]
            ),
            "basis": "SAME_ACTIONABLE_EPISODE",
        },

        "by_moneyness": {
            label: _fmt_bucket2(
                buckets[horizon_minutes]
            )
            for label, buckets
            in by_moneyness.items()
        },

        "by_strike": [
            {
                "expiry": expiry_key,
                "strike": strike_value,
                "option_type": option_type_key,
                **_fmt_bucket2(
                    buckets[horizon_minutes]
                ),
            }
            for (
                expiry_key,
                strike_value,
                option_type_key,
            ), buckets in sorted(
                by_strike.items(),
                key=lambda item: (
                    item[0][0],
                    item[0][1],
                    item[0][2],
                ),
            )
        ],

        "mfe_mae": {
            "overall": _mfe_mae_summary(
                mfe_mae_samples["overall"]
            ),
            "call_buy": _mfe_mae_summary(
                mfe_mae_samples["CALL_BUY"]
            ),
            "put_buy": _mfe_mae_summary(
                mfe_mae_samples["PUT_BUY"]
            ),
            "call_sell": _mfe_mae_summary(
                mfe_mae_samples["CALL_SELL"]
            ),
            "put_sell": _mfe_mae_summary(
                mfe_mae_samples["PUT_SELL"]
            ),
        },

        # PREMIUM_SELL_HORIZON_PATCH_20260930
        "by_horizon": {
            str(h): {
                "overall": _fmt_bucket2(
                    overall[h]
                ),
                "call_buy": _fmt_bucket2(
                    by_action["CALL_BUY"][h]
                ),
                "put_buy": _fmt_bucket2(
                    by_action["PUT_BUY"][h]
                ),
                  "call_sell": _fmt_bucket2(
                      by_action["CALL_SELL"][h]
                  ),
                  "put_sell": _fmt_bucket2(
                      by_action["PUT_SELL"][h]
                  ),
                "overall_net_of_costs":
                    _fmt_bucket2(
                        overall_net[h]
                    ),
            }
            for h in HORIZONS_MINUTES
        },

        "accuracy_basis":
            "EXPLICIT_ACTION_INDEPENDENT_EPISODES",
    }
