import asyncio


def test_builder_reuses_snapshot_ai_result_and_lifecycle_state(monkeypatch):
    import app.api.routes.analysis as analysis
    import app.api.routes.strategy as strategy

    snapshot_ts = "2026-10-12T10:35:00+05:30"
    decision = {
        "market_bias": "Sideways",
        "preferred_side": "NONE",
        "raw_preferred_side": "NONE",
        "signal_lifecycle": "WAIT",
        "signal_candidate": "NONE",
        "signal_active_side": "NONE",
        "signal_confirmations": 0,
        "signal_reversal_confirmations": 0,
        "hard_gated": False,
        "confidence": 0,
        "bull_score": 0,
        "bear_score": 0,
        "margin": 0,
        "reasons": [],
        "recommended_strike": "NONE",
    }
    market_data = {
        "timestamp": snapshot_ts,
        "market_open": True,
        "spot": {"price": 22500.0, "market_open": True},
        "decision": decision,
        "option_chain": {
            "expiry": "13OCT2026",
            "all_expiries": ["13OCT2026"],
            "data": [],
        },
        "multi_timeframe": {},
        "technicals": {},
        "support_resistance": {"support": [], "resistance": []},
        "oi_summary": {},
    }
    persisted_state = {"symbol": "NIFTY", "lifecycle": "WAIT"}
    ai_payload = {
        "symbol": "NIFTY",
        "market_bias": "Sideways",
        "preferred_side": "NONE",
        "signal_lifecycle": "WAIT",
        "signal_active_side": "NONE",
        "confidence": 0,
        "bullish_probability": 50,
        "bearish_probability": 50,
        "bull_score": 0,
        "bear_score": 0,
        "risk": "Medium",
        "pcr": 1.0,
        "vix": 12.0,
        "reason": "test result",
        "key_factors": [],
        "timeframe_trend": {},
        "forecast": "Neutral",
        "signal_action": "WAIT",
        "best_strategy": "WAIT",
    }

    class AnalyzerMustNotFetch:
        async def get_full_market_overview(self, *args, **kwargs):
            raise AssertionError("supplied market snapshot must be reused")

    async def forbidden(*args, **kwargs):
        raise AssertionError("supplied AI result/state must be reused")

    async def fake_final_action(symbol, data, state):
        assert symbol == "NIFTY"
        assert data is market_data
        assert state is persisted_state
        return {
            "best_strategy": "WAIT",
            "signal_action": "WAIT",
            "candidates": {"WAIT": 50},
            "confluence": {},
            "v3_gate": {},
        }

    monkeypatch.setattr(analysis, "get_ai_analysis", forbidden)
    monkeypatch.setattr(analysis, "load_signal_state", forbidden)
    monkeypatch.setattr(
        strategy, "resolve_final_strategy_action", fake_final_action
    )

    result = asyncio.run(
        analysis.build_ai_analysis(
            "NIFTY",
            AnalyzerMustNotFetch(),
            object(),
            market_data_override=market_data,
            ai_result_override=ai_payload,
            persisted_state_override=persisted_state,
        )
    )

    assert result["market_snapshot_timestamp"] == snapshot_ts
    assert result["signal_lifecycle"] == "WAIT"
    assert result["signal_action"] == "WAIT"
    assert result["recommended_option"] is None
