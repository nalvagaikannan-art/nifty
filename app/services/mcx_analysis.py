"""Conservative technical decision engine for Angel One MCX futures.

This module is intentionally separate from the NSE options strategy. It never
uses PCR, option-chain data, index proxies, or external commodity prices.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from math import isfinite

IST = ZoneInfo("Asia/Kolkata")


def _number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _candle_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _ema(values, period):
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi(values, period=14):
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    recent = changes[-period:]
    gains = sum(max(x, 0.0) for x in recent) / period
    losses = sum(max(-x, 0.0) for x in recent) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def _atr(candles, period=14):
    ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]
        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))
    return sum(ranges[-period:]) / period


def analyze_mcx_futures(candles, contract, now=None):
    """Return LONG_FUTURES / SHORT_FUTURES only on fresh, valid candle evidence."""
    now = (now or datetime.now(IST)).astimezone(IST)
    rows = []
    for candle in candles or []:
        if not isinstance(candle, dict):
            continue
        stamp = _candle_time(candle.get("timestamp"))
        o, h, low, close = (_number(candle.get(k)) for k in ("open", "high", "low", "close"))
        volume = _number(candle.get("volume", 0))
        if stamp is None or None in (o, h, low, close, volume):
            continue
        if min(o, h, low, close) <= 0 or h < max(o, close, low) or low > min(o, close, h):
            continue
        rows.append({"timestamp": stamp, "open": o, "high": h, "low": low,
                     "close": close, "volume": volume})
    rows.sort(key=lambda x: x["timestamp"])
    if len(rows) < 30:
        return {"action": "WAIT", "eligible": False, "reason": "At least 30 valid MCX candles are required",
                "exchange": "MCX", "instrument": "GOLDPETAL", "source": "angel_one_mcx_candles"}

    latest = rows[-1]
    age = (now - latest["timestamp"]).total_seconds()
    if age < -120 or age > 12 * 60:
        return {"action": "WAIT", "eligible": False,
                "reason": "Latest MCX candle is not fresh; no trade signal is permitted",
                "exchange": "MCX", "instrument": "GOLDPETAL",
                "last_candle_at": latest["timestamp"].isoformat(),
                "candle_age_seconds": round(age, 1),
                "source": "angel_one_mcx_candles"}

    closes = [x["close"] for x in rows]
    ema9, ema21 = _ema(closes[-60:], 9), _ema(closes[-60:], 21)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(rows, 14)
    close = latest["close"]
    tick = _number((contract or {}).get("tick_size"))
    lot = _number((contract or {}).get("lot_size"))
    if not tick or tick <= 0 or not lot or lot <= 0 or atr14 <= 0:
        return {"action": "WAIT", "eligible": False,
                "reason": "Contract tick size, lot size or ATR is invalid",
                "exchange": "MCX", "instrument": "GOLDPETAL"}

    long_ok = ema9 > ema21 and close > ema9 and 52 <= rsi14 <= 68 and close > rows[-2]["close"]
    short_ok = ema9 < ema21 and close < ema9 and 32 <= rsi14 <= 48 and close < rows[-2]["close"]
    action = "LONG_FUTURES" if long_ok else "SHORT_FUTURES" if short_ok else "WAIT"
    eligible = action != "WAIT"
    stop_distance = max(1.5 * atr14, tick)
    entry = round(close / tick) * tick
    stop = entry - stop_distance if action == "LONG_FUTURES" else entry + stop_distance
    target = entry + 2 * stop_distance if action == "LONG_FUTURES" else entry - 2 * stop_distance
    if not eligible:
        reason = "Trend, momentum and candle confirmation are not aligned"
    else:
        reason = "Fresh MCX candle; EMA trend, RSI band and price confirmation aligned"
    return {
        "action": action, "eligible": eligible, "reason": reason,
        "exchange": "MCX", "instrument": "GOLDPETAL",
        "contract": (contract or {}).get("tradingsymbol"),
        "expiry": (contract or {}).get("expiry"),
        "source": "angel_one_mcx_candles",
        "last_candle_at": latest["timestamp"].isoformat(),
        "candle_age_seconds": round(age, 1),
        "ltp_reference": close, "ema9": round(ema9, 4), "ema21": round(ema21, 4),
        "rsi14": round(rsi14, 2), "atr14": round(atr14, 4),
        "tick_size": tick, "lot_size": int(lot),
        "entry_reference": round(entry, 4) if eligible else None,
        "stop_reference": round(stop / tick) * tick if eligible else None,
        "target_reference": round(target / tick) * tick if eligible else None,
        "risk_reward": 2.0 if eligible else None,
        "execution_enabled": False,
    }
