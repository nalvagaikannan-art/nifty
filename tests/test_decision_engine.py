"""
Tests for the 20-condition weighted Decision Engine — hand-built
`market_data` fixtures representing clearly bullish / bearish / sideways /
market-closed conditions (spec §43: "bullish market, bearish market,
sideways market ... conflicting timeframe"). No network needed — this
exercises the pure scoring/aggregation logic in isolation.
"""
from app.services.decision_engine import run_decision_engine


def _base(spot: float) -> dict:
    return {
        "spot": {"price": spot, "market_open": True},
        "technicals": {
            "vwap": spot, "ema20": spot, "ema50": spot,
            "adx": 10, "di_plus": 15, "di_minus": 15,
            "atr": spot * 0.003, "supertrend": "", "volume_spike": False, "volume_ratio": 1.0,
        },
        "oi_summary": {"pe_max_oi_strike": spot * 0.99, "ce_max_oi_strike": spot * 1.01},
        "pcr": 1.0, "max_pain": spot, "oi_change": {"ce_change": 0, "pe_change": 0},
        "futures_premium": 0, "futures_premium_status": "live",
        "rsi": 50, "macd": {"macd": 0, "signal": 0, "histogram": 0}, "vix": 14,
        "global_change_pct": 0, "global_status": "live",
        "gift_nifty_change_pct": 0, "gift_status": "live",
        "fii_net_cr": 0, "fii_status": "live", "dii_net_cr": 0, "dii_status": "live",
        "support_resistance": {"support": [spot * 0.99], "resistance": [spot * 1.01]},
    }


def bullish_market_data(spot: float = 24650.0) -> dict:
    d = _base(spot)
    d["technicals"].update({
        "vwap": spot * 0.995, "ema20": spot * 0.99, "ema50": spot * 0.98,
        "adx": 30, "di_plus": 30, "di_minus": 10,
        "supertrend": "buy", "volume_spike": True, "volume_ratio": 2.0,
    })
    d["oi_summary"] = {"pe_max_oi_strike": spot * 0.99, "ce_max_oi_strike": spot * 1.05}
    d["pcr"] = 1.4
    d["max_pain"] = spot * 0.98
    d["oi_change"] = {"ce_change": -1000, "pe_change": 50000}
    d["futures_premium"] = 40
    d["rsi"] = 65
    d["macd"] = {"macd": 10, "signal": 5, "histogram": 5}
    d["global_change_pct"] = 1.0
    d["gift_nifty_change_pct"] = 0.8
    d["fii_net_cr"] = 2000
    d["dii_net_cr"] = 500
    return d


def bearish_market_data(spot: float = 24650.0) -> dict:
    d = bullish_market_data(spot)
    d["technicals"].update({
        "vwap": spot * 1.005, "ema20": spot * 1.01, "ema50": spot * 1.02,
        "di_plus": 10, "di_minus": 30, "supertrend": "sell",
    })
    d["oi_summary"] = {"pe_max_oi_strike": spot * 0.95, "ce_max_oi_strike": spot * 1.01}
    d["pcr"] = 0.6
    d["max_pain"] = spot * 1.02
    d["oi_change"] = {"ce_change": 50000, "pe_change": -1000}
    d["futures_premium"] = -40
    d["rsi"] = 30
    d["macd"] = {"macd": -10, "signal": -5, "histogram": -5}
    d["global_change_pct"] = -1.0
    d["gift_nifty_change_pct"] = -0.8
    d["fii_net_cr"] = -2000
    d["dii_net_cr"] = -500
    return d


def test_strongly_bullish_conditions_produce_call_bias():
    result = run_decision_engine(bullish_market_data())
    assert result["market_bias"] == "Bullish"
    assert result["preferred_side"] == "CALL"
    assert result["bull_score"] > result["bear_score"]
    assert result["confidence"] >= 60


def test_strongly_bearish_conditions_produce_put_bias():
    result = run_decision_engine(bearish_market_data())
    assert result["market_bias"] == "Bearish"
    assert result["preferred_side"] == "PUT"
    assert result["bear_score"] > result["bull_score"]


def test_neutral_conditions_produce_sideways_no_trade():
    """Spec §20: WAIT/NO TRADE only when direction is genuinely unclear —
    verify a flat/neutral input doesn't get forced into a directional call."""
    result = run_decision_engine(_base(24650.0))
    assert result["market_bias"] == "Sideways"
    assert result["preferred_side"] == "NONE"


def test_market_closed_forces_no_trade_regardless_of_indicators():
    """Spec §6: market-closed handling — even bullish-looking stale
    indicators must not produce a live directional call when the market
    itself is shut."""
    data = bullish_market_data()
    data["spot"]["market_open"] = False
    result = run_decision_engine(data)
    assert result["market_bias"] == "Sideways"
    assert result["preferred_side"] == "NONE"


def test_confidence_is_bounded_0_to_100():
    for data in (bullish_market_data(), bearish_market_data(), _base(24650.0)):
        result = run_decision_engine(data)
        assert 0 <= result["confidence"] <= 100
        assert 0 <= result["bullish_probability"] <= 100
        assert result["bullish_probability"] + result["bearish_probability"] == 100


def test_reasons_list_has_one_entry_per_scored_condition():
    """accuracy_engine.py / signal_accuracy.py assume exactly 20 reason
    lines in this fixed order — this test is a contract check between the
    two modules so a future edit to one doesn't silently break the other."""
    result = run_decision_engine(bullish_market_data())
    assert len(result["reasons"]) == 20


def test_high_vix_does_not_directly_flip_bull_bear_score():
    """Spec §11 (updated for review #4's market-regime-adaptive weighting):
    VIX must not FLIP the direction of the call — a bullish setup stays
    bullish (bull_score still > bear_score) whether VIX is calm or high.
    It's no longer required to produce IDENTICAL bull/bear numbers, because
    a high-VIX day now classifies into the HIGH_VOLATILITY regime, which
    deliberately reweights indicator buckets (this is the review's explicit
    ask, not a bug) — but that reweighting must never flip which side wins,
    and volatility affects confidence/regime, never the direction verdict.

    Note: _volatility_context() needs VIX AND ATR% both elevated to cross
    into the "high" regime (score >= 3) — VIX alone only contributes +2.
    So this raises both together to actually exercise that regime."""
    calm = bullish_market_data()
    calm["vix"] = 12
    volatile = bullish_market_data()
    # 28, not e.g. 35 — high enough to cross into HIGH_VOLATILITY regime
    # reweighting (>25) but below market_regime.py's own >30 "extreme,
    # unpredictable" hard NO_TRADE cutoff, which is a separate, deliberately
    # stricter gate this test isn't exercising.
    volatile["vix"] = 28
    volatile["technicals"]["atr"] = volatile["spot"]["price"] * 0.02  # ~2% ATR

    r_calm = run_decision_engine(calm)
    r_volatile = run_decision_engine(volatile)
    # Direction is preserved — still bullish, not flipped to bearish/neutral.
    assert r_calm["bull_score"] > r_calm["bear_score"]
    assert r_volatile["bull_score"] > r_volatile["bear_score"]
    assert r_calm["preferred_side"] == r_volatile["preferred_side"] == "CALL"
    # but the volatility regime label itself should differ
    assert r_calm["volatility_regime"] != r_volatile["volatility_regime"]
    assert r_volatile["volatility_regime"] == "high"
    # High-VIX day's confidence should be equal or lower — never higher —
    # than the calm day for an otherwise-identical setup.
    assert r_volatile["confidence"] <= r_calm["confidence"]


def test_scenarios_and_invalidation_always_present():
    """Spec §18/§25: every recommendation needs a stated invalidation
    condition / alternative scenario, not just a bare direction."""
    result = run_decision_engine(bullish_market_data())
    assert len(result["scenarios"]) >= 1
    for scenario in result["scenarios"]:
        assert "invalidation" in scenario
