"""
Evidence-based intraday market session state.

Stage 1:
- Observation/context only.
- Does NOT change DecisionEngine scores or signal gates.
- Uses current 5-minute/15-minute technical context and today's
  persisted/live 5-minute candles.
"""

from datetime import datetime
from statistics import median
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _parse_ts(value) -> Optional[datetime]:
    if value is None:
        return None

    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            # Internal DB timestamps are UTC-naive.
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(IST)
    except Exception:
        return None


def _phase(now: datetime) -> str:
    mins = now.hour * 60 + now.minute

    if mins < 9 * 60 + 15 or mins >= 15 * 60 + 30:
        return "CLOSED"

    if mins < 9 * 60 + 30:
        return "OPENING_DISCOVERY"

    if mins < 10 * 60 + 30:
        return "OPENING_RESOLUTION"

    if mins < 13 * 60 + 30:
        return "MIDDAY"

    if mins < 14 * 60 + 30:
        return "AFTERNOON_TRANSITION"

    if mins < 15 * 60 + 10:
        return "AFTERNOON_EXPANSION"

    return "CLOSING"


def _sign(value: float, threshold: float = 0.0) -> int:
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def _trend_from_frame(frame: Dict) -> str:
    value = str(frame.get("trend", "unavailable")).lower().strip()

    if value in {"up", "bullish", "bull"}:
        return "UP"
    if value in {"down", "bearish", "bear"}:
        return "DOWN"

    indicators = frame.get("indicators") or {}

    ema20 = float(indicators.get("ema20", 0) or 0)
    ema50 = float(indicators.get("ema50", 0) or 0)

    if ema20 > 0 and ema50 > 0:
        if ema20 > ema50:
            return "UP"
        if ema20 < ema50:
            return "DOWN"

    return "NEUTRAL"


def _extract_today_bars(frame: Dict, now: datetime) -> List[Dict]:
    timestamps = frame.get("timestamps") or []
    opens = frame.get("opens") or []
    highs = frame.get("highs") or []
    lows = frame.get("lows") or []
    closes = frame.get("closes") or []

    n = min(
        len(timestamps),
        len(highs),
        len(lows),
        len(closes),
    )

    if n <= 0:
        return []

    today = now.date()
    bars = []

    for i in range(n):
        dt = _parse_ts(timestamps[i])
        if dt is None or dt.date() != today:
            continue

        try:
            h = float(highs[i])
            l = float(lows[i])
            c = float(closes[i])

            o = (
                float(opens[i])
                if i < len(opens) and opens[i] is not None
                else c
            )

            if min(h, l, c) <= 0:
                continue

            bars.append({
                "ts": dt,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
            })
        except (TypeError, ValueError):
            continue

    bars.sort(key=lambda x: x["ts"])
    return bars


def classify_market_session_state(market_data: Dict) -> Dict:
    now = _parse_ts(market_data.get("timestamp")) or datetime.now(IST)
    phase = _phase(now)

    mtf = market_data.get("multi_timeframe") or {}
    five = mtf.get("5min") or {}
    fifteen = mtf.get("15min") or {}

    bars = _extract_today_bars(five, now)

    five_trend = _trend_from_frame(five)
    fifteen_trend = _trend_from_frame(fifteen)

    indicators = five.get("indicators") or {}
    spot_data = market_data.get("spot") or {}

    try:
        spot = float(spot_data.get("price", 0) or 0)
    except (TypeError, ValueError):
        spot = 0.0

    # LIVE_SESSION_VWAP_SOURCE_FIX_20260924
    # The 5M MTF frame is the live intraday technical source of truth.
    # Prefer its VWAP, then fall back to the top-level technical payload.
    try:
        vwap = float(indicators.get("vwap", 0) or 0)
    except (TypeError, ValueError):
        vwap = 0.0

    if vwap <= 0:
        try:
            top_technicals = market_data.get("technicals") or {}
            vwap = float(top_technicals.get("vwap", 0) or 0)
        except (TypeError, ValueError):
            vwap = 0.0

    # ── Today's opening reference ────────────────────────────────────────
    first_bar = bars[0] if bars else None
    morning_cutoff = now.replace(
        hour=10,
        minute=30,
        second=0,
        microsecond=0,
    )

    morning_bars = [
        b for b in bars
        if b["ts"] <= morning_cutoff
    ]

    opening_high = max(
        (b["high"] for b in morning_bars),
        default=0.0,
    )
    opening_low = min(
        (b["low"] for b in morning_bars),
        default=0.0,
    )

    morning_close = (
        morning_bars[-1]["close"]
        if morning_bars
        else 0.0
    )

    morning_move = (
        morning_close - first_bar["open"]
        if first_bar is not None and morning_close > 0
        else 0.0
    )

    # MORNING_MOVE_SIGNIFICANCE_20260924
    # Do not classify tiny opening noise as a meaningful morning direction.
    # Historical validation: a 10% opening-range threshold removes only
    # one tiny-move session out of 16 complete sessions.
    morning_range = opening_high - opening_low
    morning_move_ratio = (
        abs(morning_move) / morning_range
        if morning_range > 0
        else 0.0
    )

    if morning_move_ratio >= 0.10:
        morning_direction = (
            "UP" if morning_move > 0
            else "DOWN" if morning_move < 0
            else "NEUTRAL"
        )
    else:
        morning_direction = "NEUTRAL"

    # ── Recent volatility state ──────────────────────────────────────────
    recent = bars[-3:]
    previous = bars[-15:-3]

    recent_ranges = [
        b["high"] - b["low"]
        for b in recent
        if b["high"] > b["low"]
    ]

    previous_ranges = [
        b["high"] - b["low"]
        for b in previous
        if b["high"] > b["low"]
    ]

    recent_avg_range = (
        sum(recent_ranges) / len(recent_ranges)
        if recent_ranges else 0.0
    )

    previous_median_range = (
        median(previous_ranges)
        if previous_ranges else 0.0
    )

    if previous_median_range > 0:
        range_ratio = recent_avg_range / previous_median_range
    else:
        range_ratio = 1.0

    if range_ratio <= 0.75:
        volatility_state = "COMPRESSION"
    elif range_ratio >= 1.35:
        volatility_state = "EXPANSION"
    else:
        volatility_state = "NORMAL"

    # ── Recent directional move ──────────────────────────────────────────
    recent_direction = 0.0

    if len(bars) >= 9:
        prior_close = bars[-9]["close"]
        recent_close = bars[-1]["close"]
        recent_direction = recent_close - prior_close

    current_direction = (
        "UP" if recent_direction > 0
        else "DOWN" if recent_direction < 0
        else "NEUTRAL"
    )

    # ── VWAP location ────────────────────────────────────────────────────
    if spot > 0 and vwap > 0:
        vwap_relation = (
            "ABOVE" if spot > vwap
            else "BELOW" if spot < vwap
            else "AT_VWAP"
        )
    else:
        vwap_relation = "UNAVAILABLE"

    # ── Afternoon transition / reversal watch ────────────────────────────
    reversal_confirmations = 0
    reversal_reasons = []

    if phase in {"AFTERNOON_TRANSITION", "AFTERNOON_EXPANSION", "CLOSING"}:
        if (
            morning_direction in {"UP", "DOWN"}
            and current_direction in {"UP", "DOWN"}
            and current_direction != morning_direction
        ):
            reversal_confirmations += 1
            reversal_reasons.append(
                f"Morning {morning_direction} → recent {current_direction}"
            )

        if five_trend != "NEUTRAL" and fifteen_trend != "NEUTRAL":
            if five_trend != fifteen_trend:
                reversal_confirmations += 1
                reversal_reasons.append(
                    f"5M {five_trend} vs 15M {fifteen_trend} transition"
                )

        if vwap_relation != "UNAVAILABLE":
            if (
                morning_direction == "UP"
                and vwap_relation == "BELOW"
            ) or (
                morning_direction == "DOWN"
                and vwap_relation == "ABOVE"
            ):
                reversal_confirmations += 1
                reversal_reasons.append(
                    f"Price {vwap_relation} VWAP against morning direction"
                )

    if reversal_confirmations >= 2:
        transition_state = "REVERSAL_WATCH"
    elif reversal_confirmations == 1:
        transition_state = "TRANSITION"
    elif phase in {"AFTERNOON_EXPANSION", "CLOSING"}:
        transition_state = "CONTINUATION_WATCH"
    else:
        transition_state = "NONE"

    # ── Day type ─────────────────────────────────────────────────────────
    if morning_direction == "UP" and current_direction == "UP":
        day_type = "UP_TREND"
    elif morning_direction == "DOWN" and current_direction == "DOWN":
        day_type = "DOWN_TREND"
    elif (
        morning_direction in {"UP", "DOWN"}
        and current_direction in {"UP", "DOWN"}
        and morning_direction != current_direction
    ):
        day_type = "REVERSAL_DAY"
    elif volatility_state == "COMPRESSION":
        day_type = "BALANCE_DAY"
    else:
        day_type = "MIXED"

    # ── State confidence ────────────────────────────────────────────────
    confirmations = 0
    total_checks = 0

    if five_trend != "NEUTRAL":
        confirmations += 1
    total_checks += 1

    if fifteen_trend != "NEUTRAL":
        confirmations += 1
    total_checks += 1

    if volatility_state != "NORMAL":
        confirmations += 1
    total_checks += 1

    if morning_direction != "NEUTRAL":
        confirmations += 1
    total_checks += 1

    if vwap_relation != "UNAVAILABLE":
        confirmations += 1
    total_checks += 1

    confidence = round(
        confirmations / total_checks * 100
    ) if total_checks else 0

    reasons = [
        f"Phase: {phase}",
        f"Day type: {day_type}",
        f"5M={five_trend}, 15M={fifteen_trend}",
        f"Volatility={volatility_state} ({range_ratio:.2f}x)",
    ]

    if morning_direction != "NEUTRAL":
        reasons.append(
            f"Morning direction={morning_direction} ({morning_move:+.2f})"
        )

    if vwap_relation != "UNAVAILABLE":
        reasons.append(f"VWAP={vwap_relation}")

    reasons.extend(reversal_reasons)

    return {
        "phase": phase,
        "day_type": day_type,
        "trend_5m": five_trend,
        "trend_15m": fifteen_trend,
        "morning_direction": morning_direction,
        "volatility_state": volatility_state,
        "range_ratio": round(range_ratio, 3),
        "recent_direction": current_direction,
        "vwap_relation": vwap_relation,
        "transition_state": transition_state,
        "reversal_confirmations": reversal_confirmations,
        "state_confidence": confidence,
        "opening_range": {
            "high": round(opening_high, 2) if opening_high else 0.0,
            "low": round(opening_low, 2) if opening_low else 0.0,
        },
        "reasons": reasons,
        "observation_only": True,
    }
