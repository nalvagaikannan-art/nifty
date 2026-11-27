from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.mcx_analysis import analyze_mcx_futures

IST = ZoneInfo("Asia/Kolkata")


def _candles(trend="up", count=40, now=None):
    now = now or datetime.now(IST)
    rows = []
    price = 1000.0
    for i in range(count):
        if trend == "up":
            price += 2.0
        elif trend == "down":
            price -= 2.0
        stamp = now - timedelta(minutes=5 * (count - i - 1))
        rows.append({"timestamp": stamp.isoformat(), "open": price - 0.5,
                     "high": price + 1, "low": price - 1, "close": price,
                     "volume": 100})
    return rows


CONTRACT = {"tradingsymbol": "GOLDPETAL31DEC26FUT", "expiry": "2026-12-31",
            "tick_size": 1.0, "lot_size": 1}


def test_mcx_engine_requires_minimum_candle_history():
    result = analyze_mcx_futures(_candles(count=20), CONTRACT)
    assert result["action"] == "WAIT"
    assert result["eligible"] is False


def test_mcx_engine_blocks_stale_candles():
    now = datetime.now(IST)
    candles = _candles(count=40, now=now - timedelta(hours=1))
    result = analyze_mcx_futures(candles, CONTRACT, now=now)
    assert result["action"] == "WAIT"
    assert "not fresh" in result["reason"]


def test_mcx_engine_emits_only_futures_actions_and_never_executes():
    result = analyze_mcx_futures(_candles("up"), CONTRACT)
    assert result["action"] in {"LONG_FUTURES", "SHORT_FUTURES", "WAIT"}
    assert result["execution_enabled"] is False
    assert result["exchange"] == "MCX"
    assert "option_chain" not in result
