"""
Tests for the 20-condition weighted Decision Engine — hand-built
`market_data` fixtures representing clearly bullish / bearish / sideways /
market-closed conditions (spec §43: "bullish market, bearish market,
sideways market ... conflicting timeframe"). No network needed — this
exercises the pure scoring/aggregation logic in isolation.
"""
import pytest


@pytest.fixture(autouse=True)
def fixed_market_session(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_regime._time_session",
        lambda: "MID",
    )


from app.services.decision_engine import (
    run_decision_engine,
    apply_persistent_signal_lifecycle,
)


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
    d["technical_data_source"] = "intraday_5min_ohlc"
    d["multi_timeframe"] = {
        "5min": {
            "data_source": "angel_one_intraday",
            "fresh": True,
            "trend": "up",
            "indicators": d["technicals"],
        },
        "15min": {
            "data_source": "angel_one_intraday",
            "fresh": True,
            "trend": "up",
            "indicators": d["technicals"],
        },
        "1hr": {
            "data_source": "angel_one_intraday",
            "fresh": True,
            "trend": "up",
            "indicators": d["technicals"],
        },
    }
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



def _chain_quality_fixture(oi=True, volume=True):
    return {
        "available": True,
        "status": "adequate" if oi else "limited",
        "oi_usable": oi,
        "volume_usable": volume,
        "oi_coverage_pct": 100 if oi else 40,
        "volume_coverage_pct": 100 if volume else 10,
        "reason": None if oi else "OI coverage below threshold",
        "volume_reason": None if volume else "Volume coverage below threshold",
    }


def test_quality_low_oi_disables_option_flow_availability():
    from app.services.decision_engine import _data_availability
    d = {"pcr": 1.45, "option_chain_quality": _chain_quality_fixture(oi=False)}
    a = _data_availability(d, "placeholder")
    assert all(not a[k] for k in (
        "pcr", "oi_change", "max_pain", "call_writing", "put_writing"
    ))


def test_quality_low_volume_only_disables_writing_availability():
    from app.services.decision_engine import _data_availability
    d = {
        "pcr": 1.45,
        "oi_change": {"ce_change": 100, "pe_change": 150},
        "option_chain_quality": _chain_quality_fixture(volume=False),
    }
    a = _data_availability(d, "placeholder")
    assert all(a[k] for k in ("pcr", "oi_change", "max_pain"))
    assert not a["call_writing"]
    assert not a["put_writing"]


def test_quality_low_oi_neutralizes_option_flow_scores():
    d = bullish_market_data()
    strike = d["spot"]["price"] * 1.01
    d["oi_summary"]["ce_max_oi_strike"] = strike
    d["oi_change_tracked"] = {
        "available": True,
        "wall_evidence": {"ce": _wall_evidence(strike, 100, -10.0)},
    }
    d["option_chain_quality"] = _chain_quality_fixture(oi=False)
    result = run_decision_engine(d)
    assert all("OI quality limited" in reason for reason in result["reasons"][:5])
    assert "Tracked CE short buildup" not in result["reasons"][3]


def test_quality_low_volume_neutralizes_writing_scores():
    d = bullish_market_data()
    d["option_chain_quality"] = _chain_quality_fixture(volume=False)
    result = run_decision_engine(d)
    assert all("volume coverage limited" not in r.lower()
               for r in result["reasons"][:3])
    assert all("volume coverage limited" in r.lower()
               for r in result["reasons"][3:5])


def test_valid_tracked_wall_survives_low_volume_gate_but_penalty_remains():
    from app.services.decision_engine import _data_availability

    d = bullish_market_data()
    strike = d["spot"]["price"] * 1.01
    d["oi_summary"]["ce_max_oi_strike"] = strike
    d["option_chain_quality"] = _chain_quality_fixture(volume=False)
    d["oi_change_tracked"] = {
        "available": True,
        "wall_evidence": {"ce": _wall_evidence(strike, 100, -10.0)},
    }
    result = run_decision_engine(d)

    assert "Tracked CE short buildup" in result["reasons"][3]
    assert "completeness penalty retained" in result["reasons"][3].lower()
    assert "volume coverage limited" in result["reasons"][4].lower()

    availability = _data_availability(d, d.get("technical_data_source", ""))
    assert availability["call_writing"] is False
    assert availability["put_writing"] is False


def test_invalid_tracked_wall_price_falls_back_to_legacy():
    from app.services.decision_engine import _score_call_writing

    summary = {
        "ce_max_oi_strike": 22700, "spot": 22622,
        "ce_oi_chg_atm": 1200, "ce_ltp_chg_atm": None,
    }
    evidence = _wall_evidence(22700, 100, -10.0)
    evidence["current_price"] = 0
    result = _score_call_writing({
        **summary, "_wall_evidence": {"ce": evidence}
    })
    assert "ATM option price change unavailable" in result[2]


def test_quality_missing_preserves_legacy_availability():
    from app.services.decision_engine import _data_availability
    a = _data_availability(
        {
            "pcr": 1.45,
            "oi_change": {"ce_change": 100, "pe_change": 50},
        },
        "placeholder",
    )
    assert all(a[k] for k in (
        "pcr", "oi_change", "max_pain", "call_writing", "put_writing"
    ))

def test_strongly_bullish_conditions_produce_call_bias():
    result = run_decision_engine(bullish_market_data())
    assert result["market_bias"] == "Bullish"
    assert result["raw_preferred_side"] == "CALL"
    assert result["bull_score"] > result["bear_score"]
    assert result["confidence"] >= 60


def test_strongly_bearish_conditions_produce_put_bias():
    result = run_decision_engine(bearish_market_data())
    assert result["market_bias"] == "Bearish"
    assert result["raw_preferred_side"] == "PUT"
    assert result["bear_score"] > result["bull_score"]


def test_mtf_observation_log_does_not_change_decision(caplog):
    import logging

    data = bullish_market_data()
    data["symbol"] = "NIFTY"
    data["timestamp"] = "2026-10-05T10:15:00+05:30"

    with caplog.at_level(logging.INFO, logger="app.services.decision_engine"):
        result = run_decision_engine(data)

    assert result["raw_preferred_side"] == "CALL"
    assert result["market_bias"] == "Bullish"

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.services.decision_engine"
    ]
    logs = [m for m in messages if m.startswith("MTF_OBSERVATION ")]
    assert logs

    line = logs[-1]
    assert "symbol=NIFTY" in line
    assert "technical_source=intraday_5min_ohlc" in line
    assert "live_valid=True" in line
    assert "5min=" in line
    assert "15min=" in line
    assert "1hr=" in line


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
    """The first 20 reason entries are the fixed indicator slots used by
    accuracy_engine.py; later entries may contain guard/session/MTF diagnostics."""

    result = run_decision_engine(bullish_market_data())
    assert len(result["reasons"]) >= 20


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
    assert r_calm["raw_preferred_side"] == r_volatile["raw_preferred_side"] == "CALL"
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

def _lifecycle_decision(side: str, margin: float, market_open: bool = True) -> dict:
    return {
        "preferred_side": side,
        "margin": margin,
        "market_open": market_open,
        "market_regime_no_trade": False,
    }


def test_persistent_lifecycle_requires_three_spaced_call_confirmations():
    state = None

    decision = _lifecycle_decision("CALL", 20)

    out, state = apply_persistent_signal_lifecycle(decision, state, 1000)
    assert out["signal_lifecycle"] == "WATCH_CALL"
    assert out["signal_candidate"] == "CALL"
    assert out["signal_confirmations"] == 1
    assert out["signal_active_side"] == "NONE"

    out, state = apply_persistent_signal_lifecycle(decision, state, 1046)
    assert out["signal_lifecycle"] == "WATCH_CALL"
    assert out["signal_confirmations"] == 2
    assert out["signal_active_side"] == "NONE"

    out, state = apply_persistent_signal_lifecycle(decision, state, 1092)
    assert out["signal_lifecycle"] == "CONFIRMED_CALL"
    assert out["signal_confirmations"] == 3
    assert out["signal_active_side"] == "CALL"
    assert out["preferred_side"] == "CALL"


def test_persistent_lifecycle_requires_two_spaced_reversal_confirmations():
    state = {
        "active_side": "CALL",
        "candidate_side": "CALL",
        "confirmations": 3,
        "reversal_confirmations": 0,
        "lifecycle": "HOLD_CALL",
        "last_evaluation_at": 1000,
    }

    decision = _lifecycle_decision("PUT", -20)

    out, state = apply_persistent_signal_lifecycle(decision, state, 1046)
    assert out["signal_lifecycle"] == "HOLD_CALL_REVERSAL_WATCH_PUT"
    assert out["signal_reversal_confirmations"] == 1
    assert out["signal_active_side"] == "CALL"

    out, state = apply_persistent_signal_lifecycle(decision, state, 1092)
    assert out["signal_lifecycle"] == "CONFIRMED_PUT"
    assert out["signal_reversal_confirmations"] == 2
    assert out["signal_active_side"] == "PUT"
    assert out["preferred_side"] == "PUT"


def test_persistent_lifecycle_weak_opposite_signal_does_not_reverse():
    state = {
        "active_side": "CALL",
        "candidate_side": "CALL",
        "confirmations": 3,
        "reversal_confirmations": 1,
        "lifecycle": "HOLD_CALL_REVERSAL_WATCH_PUT",
        "last_evaluation_at": 1000,
    }

    decision = _lifecycle_decision("PUT", -10)

    out, state = apply_persistent_signal_lifecycle(decision, state, 1046)
    assert out["signal_lifecycle"] == "WATCH_PUT"
    assert out["signal_reversal_confirmations"] == 0
    assert out["signal_active_side"] == "NONE"
    assert out["preferred_side"] == "NONE"


def test_persistent_lifecycle_same_active_side_remains_hold():
    state = {
        "active_side": "CALL",
        "candidate_side": "CALL",
        "confirmations": 3,
        "reversal_confirmations": 0,
        "lifecycle": "HOLD_CALL",
        "last_evaluation_at": 1000,
    }

    decision = _lifecycle_decision("CALL", 20)

    out, state = apply_persistent_signal_lifecycle(decision, state, 1046)
    assert out["signal_lifecycle"] == "HOLD_CALL"
    assert out["signal_confirmations"] == 3
    assert out["signal_reversal_confirmations"] == 0
    assert out["signal_active_side"] == "CALL"
    assert out["preferred_side"] == "CALL"


def test_persistent_lifecycle_hard_gate_clears_directional_state():
    state = {
        "active_side": "CALL",
        "candidate_side": "CALL",
        "confirmations": 3,
        "reversal_confirmations": 1,
        "lifecycle": "HOLD_CALL",
        "last_evaluation_at": 1000,
    }

    decision = _lifecycle_decision("CALL", 20, market_open=False)

    out, state = apply_persistent_signal_lifecycle(decision, state, 1046)
    assert out["signal_lifecycle"] == "WAIT"
    assert out["signal_active_side"] == "NONE"
    assert out["signal_candidate"] == "NONE"
    assert out["signal_confirmations"] == 0
    assert out["signal_reversal_confirmations"] == 0
    assert out["preferred_side"] == "NONE"


def test_persistent_lifecycle_hard_gate_overrides_older_duplicate_snapshot_without_rewinding_timestamp():
    state = {
        "active_side": "CALL",
        "candidate_side": "CALL",
        "confirmations": 3,
        "reversal_confirmations": 1,
        "lifecycle": "HOLD_CALL",
        "last_evaluation_at": 1100,
        "last_confirmation_at": 900,
    }

    decision = _lifecycle_decision("CALL", 20)
    decision["market_regime_no_trade"] = True

    out, next_state = apply_persistent_signal_lifecycle(
        decision, state, 1046
    )

    assert out["signal_lifecycle"] == "WAIT"
    assert out["signal_active_side"] == "NONE"
    assert out["signal_candidate"] == "NONE"
    assert out["signal_confirmations"] == 0
    assert out["signal_reversal_confirmations"] == 0
    assert out["preferred_side"] == "NONE"

    # The stale response is gated, but must not overwrite the newer state.
    assert next_state["lifecycle"] == "HOLD_CALL"
    assert next_state["active_side"] == "CALL"
    assert next_state["last_evaluation_at"] == 1100


# DB_MTF_LINEAGE_REGRESSION_20260930
def test_mtf_live_valid_rejects_history_db_fallback_even_when_fresh():
    from app.services.decision_engine import _mtf_live_valid

    mtf = {
        "5min": {
            "data_source": "history_db_intraday_ohlc",
            "fresh": True,
        },
        "15min": {
            "data_source": "history_db_intraday_ohlc",
            "fresh": True,
        },
        "1hr": {
            "data_source": "history_db_intraday_ohlc",
            "fresh": True,
        },
    }

    assert _mtf_live_valid(mtf) is False

# OI_FALLBACK_REASON_REGRESSION_20261022
def test_call_writing_fallback_explains_missing_price_change():
    from app.services.decision_engine import WEIGHTS, _score_call_writing

    direction, score, reason = _score_call_writing({
        "ce_max_oi_strike": 22700,
        "spot": 22622,
        "ce_oi_chg_atm": 1200,
        "ce_ltp_chg_atm": None,
    })

    assert direction == "bear"
    assert score == int(WEIGHTS["call_writing"] * 0.5)
    assert "ATM option price change unavailable" in reason
    assert "proximity-only fallback" in reason
    assert "OI change unavailable" not in reason


def test_put_writing_fallback_explains_missing_price_change():
    from app.services.decision_engine import WEIGHTS, _score_put_writing

    direction, score, reason = _score_put_writing({
        "pe_max_oi_strike": 22550,
        "spot": 22622,
        "pe_oi_chg_atm": 900,
        "pe_ltp_chg_atm": None,
    })

    assert direction == "bull"
    assert score == int(WEIGHTS["put_writing"] * 0.5)
    assert "ATM option price change unavailable" in reason
    assert "proximity-only fallback" in reason
    assert "OI change unavailable" not in reason

# TRACKED_WALL_SCORING_TESTS_20261022
def _wall_evidence(strike, oi_change, price_change):
    current = max(100.0, 100.0 + float(price_change))
    previous = current - float(price_change)
    return {
        "available": True, "strike": strike, "oi_change": oi_change,
        "price_change": price_change,
        "current_price": current, "previous_price": previous,
    }


def test_ce_tracked_wall_short_buildup_is_used():
    from app.services.decision_engine import WEIGHTS, _score_call_writing
    summary = {"ce_max_oi_strike": 22700, "spot": 22622,
               "ce_oi_chg_atm": 1200, "ce_ltp_chg_atm": None}
    result = _score_call_writing(
        {**summary, "_wall_evidence": {"ce": _wall_evidence(22700, 100, -10)}}
    )
    assert result[0] == "bear"
    assert result[1] == WEIGHTS["call_writing"]
    assert "Tracked CE short buildup" in result[2]


def test_pe_tracked_wall_short_buildup_is_used():
    from app.services.decision_engine import WEIGHTS, _score_put_writing
    summary = {"pe_max_oi_strike": 22550, "spot": 22622,
               "pe_oi_chg_atm": 900, "pe_ltp_chg_atm": None}
    result = _score_put_writing(
        {**summary, "_wall_evidence": {"pe": _wall_evidence(22550, 200, -5)}}
    )
    assert result[0] == "bull"
    assert result[1] == WEIGHTS["put_writing"]
    assert "Tracked PE short buildup" in result[2]


def test_zero_or_unmatched_tracked_wall_does_not_override_legacy():
    from app.services.decision_engine import WEIGHTS, _score_call_writing
    summary = {"ce_max_oi_strike": 22700, "spot": 22622,
               "ce_oi_chg_atm": 1200, "ce_ltp_chg_atm": None}
    zero = _score_call_writing({
        **summary, "_wall_evidence": {"ce": _wall_evidence(22700, 0, -10)}
    })
    unmatched = _score_call_writing({
        **summary, "_wall_evidence": {"ce": _wall_evidence(22650, 100, -10)}
    })
    assert zero[0:2] == ("neutral", 0)
    assert "zero OI or price change" in zero[2]
    assert unmatched[0] == "bear"
    assert unmatched[1] == int(WEIGHTS["call_writing"] * 0.5)
    assert "ATM option price change unavailable" in unmatched[2]


def test_decision_engine_passes_wall_evidence_in_summary_copy(monkeypatch):
    import app.services.decision_engine as engine
    data = bullish_market_data()
    evidence = {"ce": _wall_evidence(24700, 100, -10)}
    data["oi_change_tracked"] = {"wall_evidence": evidence}
    seen = {}

    def fake_call(summary):
        seen["call"] = summary
        return "neutral", 0, "test"

    def fake_put(summary):
        seen["put"] = summary
        return "neutral", 0, "test"

    monkeypatch.setattr(engine, "_score_call_writing", fake_call)
    monkeypatch.setattr(engine, "_score_put_writing", fake_put)
    engine.run_decision_engine(data)

    assert seen["call"]["_wall_evidence"] is evidence
    assert seen["put"]["_wall_evidence"] is evidence
    assert "_wall_evidence" not in data["oi_summary"]

def test_missing_or_none_spot_market_open_fails_closed_for_all_supported_symbols():
    from app.services.market_regime import classify_market_regime

    for symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        for status in ("missing", None):
            data = bullish_market_data()
            data["symbol"] = symbol

            if status == "missing":
                data["spot"].pop("market_open", None)
            else:
                data["spot"]["market_open"] = None

            decision = run_decision_engine(data)
            regime = classify_market_regime(data)

            assert decision["market_open"] is False, (symbol, status)
            assert decision["hard_gated"] is True, (symbol, status)
            assert decision["preferred_side"] == "NONE", (symbol, status)
            assert regime["regime"] == "NO_TRADE", (symbol, status)
            assert regime["hard_no_trade"] is True, (symbol, status)


def test_persistent_lifecycle_missing_market_open_fails_closed():
    for status in ("missing", None):
        decision = _lifecycle_decision("CALL", 20)

        if status == "missing":
            decision.pop("market_open", None)
        else:
            decision["market_open"] = None

        out, _ = apply_persistent_signal_lifecycle(decision, {}, 1046)

        assert out["preferred_side"] == "NONE", status
        assert out["signal_lifecycle"] == "WAIT", status
        assert out["signal_active_side"] == "NONE", status
