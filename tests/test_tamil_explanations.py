import pytest

from app.services.tamil_explainer import build_tamil_indicators
from app.services.market_regime import _market_reason_ta, classify_market_regime


def has_tamil(text):
    return any("\u0B80" <= ch <= "\u0BFF" for ch in str(text))


def make_data():
    return {
        "spot": {"price": 24000},
        "technicals": {
            "ema20": 23900, "ema50": 23800, "vwap": 23950,
            "adx": 28, "di_plus": 30, "di_minus": 12,
            "atr": 180, "supertrend": "buy",
            "volume_spike": True, "volume_ratio": 1.8,
        },
        "oi_summary": {"ce_max_oi_strike": 24200, "pe_max_oi_strike": 23800},
        "oi_change_tracked": {
            "available": True, "ce_oi_change": 100, "pe_oi_change": 500,
        },
        "pcr": 1.4, "max_pain": 23900,
        "futures_premium": 40, "futures_premium_status": "live",
        "rsi": 72, "macd": {"macd": 2, "signal": 1, "histogram": 1},
        "vix": 24,
        "global_status": "live", "global_change_pct": 1.0,
        "gift_status": "live", "gift_nifty_change_pct": 0.5,
        "fii_status": "live", "fii_net_cr": 600,
        "dii_status": "live", "dii_net_cr": -600,
    }


def test_all_twenty_indicators_have_tamil_explanations():
    items = build_tamil_indicators(make_data(), {})
    assert len(items) == 20
    for item in items:
        assert has_tamil(item["explanation_ta"]), item["id"]


def test_indicator_values_and_directions_are_unchanged():
    items = {x["id"]: x for x in build_tamil_indicators(make_data(), {})}
    assert items["pcr"]["value_display"] == "1.400"
    assert items["pcr"]["direction"] == "bull"
    assert items["oi_change"]["value_display"] == "CE +100 / PE +500"
    assert items["oi_change"]["direction"] == "bull"
    assert items["atr"]["direction"] == "neutral"


@pytest.mark.parametrize(
    "reason",
    [
        "Market is closed",
        "Opening 30 min — false signals likely, wait for range to form",
        "Closing 30 min -- little time left for a fresh signal to play out, avoid new entries",
        "Session: PRE_MARKET — market may be closed",
        "VIX 31.2 > 30 — extreme fear, unpredictable moves",
        "⚠️ VIX 31.2 > 30 — extreme fear, unpredictable moves",
        "Expiry in 2 day(s) — high gamma, pin risk",
        "Gap up +1.8% — breakout possible",
        "Gap down -1.8% — breakdown possible",
        "VIX 26.0 > 25 — high volatility regime",
        "Gap up +2.0% with ADX 25.0 — breakout",
        "Gap down -2.0% with ADX 25.0 — breakdown",
        "ADX 28.0, +DI 32.0 > -DI 10.0 — strong uptrend",
        "EMA20 24000 > EMA50 23900 — trend confirmed",
        "ADX 28.0, -DI 32.0 > +DI 10.0 — strong downtrend",
        "EMA20 23800 < EMA50 23900 — downtrend confirmed",
        "ADX 16.0 < 18 — range bound, no trend",
        "Expiry in 2 day(s) — gamma risk dominant",
        "VIX 11.0 low + ATR 0.4% — compressed, breakout possible",
        "ADX 20.0 — no clear trend, treat as range",
        "⚠️ Confluence quality LOW — reduce size or wait",
    ],
)
def test_market_regime_reasons_have_tamil_copy(reason):
    translated = _market_reason_ta(reason)
    assert translated != reason
    assert has_tamil(translated)


def test_market_closed_no_trade_gate_is_unchanged():
    result = classify_market_regime({
        "spot": {"price": 24000, "market_open": False}
    })
    assert result["no_trade"] is True
    assert result["hard_no_trade"] is True
    assert result["reasons"] == ["Market is closed"]
    assert result["reasons_ta"] == ["சந்தை தற்போது மூடப்பட்டுள்ளது."]
