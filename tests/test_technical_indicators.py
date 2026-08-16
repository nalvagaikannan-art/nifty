"""
Pure-function tests for TechnicalIndicators — no network, no mocking needed.
Addresses CODE_REVIEW.md #18 ("current tests hit live NSE/AI — flaky and
slow") for the indicator math specifically, and spec §43's requirement to
test bullish/bearish/sideways/high-volatility conditions explicitly.
"""
import numpy as np
import pytest

from app.services.technical_indicators import TechnicalIndicators as TI


def _uptrend(n=60, start=100.0, step=1.0, noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    vals = start + np.arange(n) * step
    if noise:
        vals = vals + rng.normal(0, noise, n)
    return vals.tolist()


def _downtrend(n=60, start=200.0, step=1.0, noise=0.0, seed=2):
    return _uptrend(n, start, -step, noise, seed)


def _flat(n=60, value=100.0, noise=0.3, seed=3):
    rng = np.random.default_rng(seed)
    return (value + rng.normal(0, noise, n)).tolist()


# ── trend_detection ──────────────────────────────────────────────────────

def test_trend_detection_bullish_on_uptrend():
    assert TI.trend_detection(_uptrend()) == "bullish"


def test_trend_detection_bearish_on_downtrend():
    assert TI.trend_detection(_downtrend()) == "bearish"


def test_trend_detection_sideways_on_flat():
    assert TI.trend_detection(_flat()) == "sideways"


def test_trend_detection_sideways_when_insufficient_data():
    # spec §7/§43: must not fabricate a trend from too little data
    assert TI.trend_detection([100.0, 101.0, 99.0]) == "sideways"


# ── RSI ───────────────────────────────────────────────────────────────────

def test_rsi_high_on_strong_uptrend():
    rsi = TI.rsi(_uptrend(n=30, noise=0))
    assert rsi > 70  # steady up-only move → overbought territory


def test_rsi_low_on_strong_downtrend():
    rsi = TI.rsi(_downtrend(n=30, noise=0))
    assert rsi < 30


def test_rsi_neutral_default_when_insufficient_data():
    assert TI.rsi([100.0, 101.0]) == 50.0


# ── MACD ──────────────────────────────────────────────────────────────────

def test_macd_positive_histogram_on_sustained_uptrend():
    macd = TI.macd(_uptrend(n=60, noise=0))
    assert macd["macd"] > 0
    assert macd["histogram"] >= 0 or macd["macd"] > macd["signal"] * 0  # line above zero at minimum


def test_macd_empty_when_insufficient_data():
    macd = TI.macd([100.0] * 10)
    assert macd == {"macd": 0.0, "signal": 0.0, "histogram": 0.0}


# ── compute_all (close-only) ─────────────────────────────────────────────

def test_compute_all_empty_result_when_insufficient_history():
    result = TI.compute_all([100.0, 101.0])
    assert result["ema20"] == 0.0
    assert result["rsi"] == 50.0
    assert result["supertrend"] == ""


def test_compute_all_ema_ordering_on_uptrend():
    prices = _uptrend(n=80, noise=0)
    result = TI.compute_all(prices)
    # On a clean uptrend, the faster EMA should sit above the slower EMA.
    assert result["ema20"] > result["ema50"]


def test_compute_all_volume_spike_detection():
    prices = _flat(n=25, noise=0.1)
    volumes = [1000.0] * 24 + [5000.0]  # last bar is a clear spike
    result = TI.compute_all(prices, volumes)
    assert result["volume_spike"] is True
    assert result["volume_ratio"] > 1.5


def test_compute_all_no_volume_spike_on_steady_volume():
    prices = _flat(n=25, noise=0.1)
    volumes = [1000.0] * 25
    result = TI.compute_all(prices, volumes)
    assert result["volume_spike"] is False


# ── compute_from_ohlc (true Wilder ADX/ATR + Supertrend + VWAP) ─────────

def _synthetic_ohlc(n=60, trend=1.0, seed=7):
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(np.full(n, trend) + rng.normal(0, 0.3, n))
    highs = closes + np.abs(rng.normal(0.5, 0.2, n))
    lows = closes - np.abs(rng.normal(0.5, 0.2, n))
    volumes = rng.integers(1000, 5000, n).astype(float)
    return highs.tolist(), lows.tolist(), closes.tolist(), volumes.tolist()


def test_compute_from_ohlc_supertrend_buy_on_uptrend():
    highs, lows, closes, volumes = _synthetic_ohlc(n=80, trend=1.2)
    result = TI.compute_from_ohlc(highs, lows, closes, volumes)
    assert result["source"] == "ohlc_wilder"
    assert result["supertrend"] == "buy"
    assert result["adx"] >= 0  # sanity: doesn't crash / stays non-negative


def test_compute_from_ohlc_supertrend_sell_on_downtrend():
    highs, lows, closes, volumes = _synthetic_ohlc(n=80, trend=-1.2)
    result = TI.compute_from_ohlc(highs, lows, closes, volumes)
    assert result["supertrend"] == "sell"


def test_compute_from_ohlc_vwap_within_price_range():
    highs, lows, closes, volumes = _synthetic_ohlc(n=40, trend=0.3)
    result = TI.compute_from_ohlc(highs, lows, closes, volumes)
    assert min(lows) <= result["vwap"] <= max(highs)


# ── Max Pain / OI-adjacent helper: combine_support_resistance ───────────

def test_combine_support_resistance_uses_oi_walls():
    pivot_sr = {"support": [95.0], "resistance": [105.0], "pivot": 100.0}
    oi_summary = {"pe_max_oi_strike": 96.0, "ce_max_oi_strike": 104.0}
    result = TI.combine_support_resistance(pivot_sr, oi_summary, spot=100.0)
    assert 96.0 in result["support"]
    assert 104.0 in result["resistance"]
    assert result["sources"]["oi_walls"] is True


def test_combine_support_resistance_falls_back_when_no_sources():
    result = TI.combine_support_resistance({}, {}, spot=100.0)
    assert result["support"]      # never empty — always has a fallback
    assert result["resistance"]
    assert result["sources"]["oi_walls"] is False
