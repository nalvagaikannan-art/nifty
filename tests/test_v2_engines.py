"""
V2 Engine Tests
===============
Confluence, Market Regime, Trade Levels, Risk Engine
"""

import pytest
from app.services.confluence_engine import run_confluence_engine
from app.services.market_regime import classify_market_regime
from app.services.trade_levels import calculate_trade_levels
from app.services.risk_engine import assess_risk


# ── Sample market data builders ───────────────────────────────────────────

def make_market_data(
    spot=24000, prev_close=23900, adx=30, di_plus=28, di_minus=18,
    rsi=62, vix=14, pcr=1.2,
    ema20=23950, ema50=23800, vwap=23980,
    atr=120, supertrend="bullish",
    volume_spike=True, volume_ratio=2.1,
    global_change=0.6, fii=800, market_open=True,
    macd_line=15, macd_signal=10,
    ce_change=50000, pe_change=80000,
    ce_max_oi=24200, pe_max_oi=23800,
    support=None, resistance=None,
):
    return {
        "spot": {"price": spot, "prev_close": prev_close, "market_open": market_open},
        "technicals": {
            "adx": adx, "di_plus": di_plus, "di_minus": di_minus,
            "rsi": rsi, "ema20": ema20, "ema50": ema50, "vwap": vwap,
            "atr": atr, "supertrend": supertrend,
            "volume_spike": volume_spike, "volume_ratio": volume_ratio,
            "macd": {"macd": macd_line, "signal": macd_signal, "histogram": macd_line - macd_signal},
        },
        "vix":            vix,
        "pcr":            pcr,
        "rsi":            rsi,
        "macd":           {"macd": macd_line, "signal": macd_signal, "histogram": macd_line - macd_signal},
        "oi_change":      {"ce_change": ce_change, "pe_change": pe_change},
        "oi_summary":     {"ce_max_oi_strike": ce_max_oi, "pe_max_oi_strike": pe_max_oi, "atm_iv": 18},
        "global_change_pct": global_change,
        "global_status":  "live",
        "fii_net_cr":     fii,
        "fii_status":     "live",
        "dii_net_cr":     200,
        "dii_status":     "live",
        "decision":       {"market_bias": "Bullish", "preferred_side": "CALL", "confidence": 70, "margin": 25},
        "support_resistance": {
            "support":    support or [23700, 23500],
            "resistance": resistance or [24200, 24500],
        },
        "option_chain": {"expiry": "2026-08-28"},
    }


# ── Confluence Engine Tests ───────────────────────────────────────────────

class TestConfluenceEngine:

    def test_strong_bullish(self):
        data   = make_market_data()
        result = run_confluence_engine(data)
        assert result["direction"] == "BULLISH"
        assert result["bull_score"] > result["bear_score"]
        assert result["confluence_score"] >= 40
        assert result["quality"] in ("HIGH", "MEDIUM")
        assert len(result["factors"]) > 0

    def test_strong_bearish(self):
        data = make_market_data(
            spot=24000, prev_close=24200,   # gap down
            adx=28, di_plus=14, di_minus=26,
            rsi=38, vix=22, pcr=0.7,
            ema20=24100, ema50=24200,       # below both
            supertrend="bearish",
            ce_change=90000, pe_change=20000,
            global_change=-0.7, fii=-900,
        )
        result = run_confluence_engine(data)
        assert result["direction"] == "BEARISH"
        assert result["bear_score"] > result["bull_score"]

    def test_range_market(self):
        data = make_market_data(
            adx=12, di_plus=16, di_minus=15,
            volume_spike=False, rsi=50, pcr=1.0,
            global_change=0.0, fii=0,
        )
        result = run_confluence_engine(data)
        assert result["confluence_score"] < 60   # weak confluence
        assert result["quality"] in ("LOW", "MEDIUM")

    def test_high_vix(self):
        data = make_market_data(vix=28)
        result = run_confluence_engine(data)
        # VIX factor should be neutral
        vix_factor = next((f for f in result["factors"] if f["id"] == "india_vix"), None)
        assert vix_factor is not None
        assert vix_factor["direction"] == "neutral"

    def test_no_double_counting(self):
        data   = make_market_data()
        result = run_confluence_engine(data)
        factor_ids = [f["id"] for f in result["factors"]]
        assert len(factor_ids) == len(set(factor_ids)), "Duplicate factors found"

    def test_output_structure(self):
        result = run_confluence_engine(make_market_data())
        for key in ["bull_score", "bear_score", "confluence_score", "direction",
                    "quality", "agreement_count", "total_factors", "factors"]:
            assert key in result, f"Missing key: {key}"

    def test_missing_data_graceful(self):
        result = run_confluence_engine({})
        assert "direction" in result
        assert result["direction"] in ("BULLISH", "BEARISH", "NEUTRAL")


# ── Market Regime Tests ───────────────────────────────────────────────────

class TestMarketRegime:

    def test_trend_up(self):
        data   = make_market_data(adx=28, di_plus=26, di_minus=14)
        result = classify_market_regime(data)
        assert result["regime"] == "TREND_UP"
        assert result["option_buying_allowed"] is True

    def test_trend_down(self):
        data = make_market_data(adx=27, di_plus=14, di_minus=26, spot=24000, prev_close=24200)
        result = classify_market_regime(data)
        assert result["regime"] == "TREND_DOWN"
        assert result["option_buying_allowed"] is True

    def test_range_low_adx(self):
        data   = make_market_data(adx=12)
        result = classify_market_regime(data)
        assert result["regime"] == "RANGE"
        assert result["option_buying_allowed"] is False
        assert result["option_selling_allowed"] is True

    def test_high_volatility(self):
        data   = make_market_data(vix=28)
        result = classify_market_regime(data)
        assert result["regime"] == "HIGH_VOLATILITY"
        assert result["no_trade"] is True
        assert result["option_buying_allowed"] is False

    def test_market_closed(self):
        data   = make_market_data(market_open=False)
        result = classify_market_regime(data)
        assert result["regime"] == "NO_TRADE"
        assert result["no_trade"] is True

    def test_breakout(self):
        data = make_market_data(spot=24000, prev_close=23640, adx=22)  # +1.5% gap
        result = classify_market_regime(data)
        assert result["regime"] in ("BREAKOUT", "TREND_UP")

    def test_extreme_vix_no_trade(self):
        data   = make_market_data(vix=32)
        result = classify_market_regime(data)
        assert result["no_trade"] is True

    def test_output_has_required_keys(self):
        result = classify_market_regime(make_market_data())
        for key in ["regime", "confidence", "option_buying_allowed",
                    "option_selling_allowed", "preferred_strategy", "no_trade"]:
            assert key in result, f"Missing key: {key}"


# ── Trade Levels Tests ────────────────────────────────────────────────────

class TestTradeLevels:

    def test_bullish_levels(self):
        data   = make_market_data()
        result = calculate_trade_levels(data, "bullish", spot=24000, option_ltp=150)
        assert result["direction"] == "bullish"
        assert result["stop_loss_spot"] < 24000
        assert result["target_1_spot"] > 24000
        assert result["target_2_spot"] > result["target_1_spot"]
        assert result["trigger"] > 0

    def test_bearish_levels(self):
        data   = make_market_data()
        result = calculate_trade_levels(data, "bearish", spot=24000, option_ltp=150)
        assert result["direction"] == "bearish"
        assert result["stop_loss_spot"] > 24000
        assert result["target_1_spot"] < 24000
        assert result["target_2_spot"] < result["target_1_spot"]

    def test_rr_at_least_2(self):
        data   = make_market_data()
        result = calculate_trade_levels(data, "bullish", spot=24000, option_ltp=150)
        assert result["rr_ratio"] >= 2.0

    def test_option_levels_present_when_ltp_provided(self):
        data   = make_market_data()
        result = calculate_trade_levels(data, "bullish", spot=24000, option_ltp=120)
        assert result["option_entry"] is not None
        assert result["option_sl"] is not None
        assert result["option_t1"] is not None

    def test_option_sl_above_floor(self):
        data   = make_market_data()
        result = calculate_trade_levels(data, "bullish", spot=24000, option_ltp=100)
        # SL must be at least 40% of premium floor
        assert result["option_sl"] >= 100 * 0.40

    def test_no_option_ltp(self):
        data   = make_market_data()
        result = calculate_trade_levels(data, "bullish", spot=24000, option_ltp=0)
        assert result["option_entry"] is None
        assert result["option_sl"] is None

    def test_invalid_spot_returns_gracefully(self):
        data   = make_market_data(spot=0)
        result = calculate_trade_levels(data, "bullish", spot=0, option_ltp=0)
        assert "direction" in result


# ── Risk Engine Tests ─────────────────────────────────────────────────────

class TestRiskEngine:

    def _good_params(self, **overrides):
        params = dict(
            capital=200000,
            entry_price=150,
            stop_loss_price=100,
            rr_ratio=2.0,
            market_regime="TREND_UP",
            vix=14,
            days_to_expiry=7,
            confluence_quality="HIGH",
            signal_strength=75,
            open_positions=0,
            daily_pnl=0,
        )
        params.update(overrides)
        return params

    def test_normal_trade_allowed(self):
        result = assess_risk(**self._good_params())
        assert result["allowed"] is True
        assert result["lots"] >= 1
        assert result["max_loss"] > 0

    def test_daily_loss_exceeded(self):
        result = assess_risk(**self._good_params(capital=200000, daily_pnl=-7000))
        assert result["allowed"] is False
        assert any("daily" in r.lower() for r in result["no_trade_reasons"])

    def test_too_many_positions(self):
        result = assess_risk(**self._good_params(open_positions=3))
        assert result["allowed"] is False
        assert any("position" in r.lower() for r in result["no_trade_reasons"])

    def test_invalid_sl(self):
        result = assess_risk(**self._good_params(stop_loss_price=0))
        assert result["allowed"] is False

    def test_sl_above_entry_rejected(self):
        result = assess_risk(**self._good_params(entry_price=100, stop_loss_price=120))
        assert result["allowed"] is False

    def test_poor_liquidity_zero_ltp(self):
        result = assess_risk(**self._good_params(entry_price=0))
        assert result["allowed"] is False

    def test_extreme_vix(self):
        result = assess_risk(**self._good_params(vix=32))
        assert result["allowed"] is False

    def test_high_volatility_regime(self):
        result = assess_risk(**self._good_params(market_regime="HIGH_VOLATILITY"))
        assert result["allowed"] is False

    def test_expiry_gamma_regime(self):
        result = assess_risk(**self._good_params(market_regime="EXPIRY_HIGH_GAMMA"))
        assert result["allowed"] is False

    def test_low_confluence_rejected(self):
        result = assess_risk(**self._good_params(confluence_quality="LOW"))
        assert result["allowed"] is False

    def test_low_rr_rejected(self):
        result = assess_risk(**self._good_params(rr_ratio=1.0))
        assert result["allowed"] is False

    def test_signal_strength_does_not_increase_position(self):
        r_low  = assess_risk(**self._good_params(signal_strength=50))
        r_high = assess_risk(**self._good_params(signal_strength=95))
        # Lots should be the same regardless of signal strength
        assert r_low["lots"] == r_high["lots"]

    def test_position_size_from_capital(self):
        r_small = assess_risk(**self._good_params(capital=100000))
        r_large = assess_risk(**self._good_params(capital=500000))
        # More capital → more lots
        assert r_large["lots"] >= r_small["lots"]
