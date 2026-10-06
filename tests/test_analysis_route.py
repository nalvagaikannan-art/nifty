import asyncio


def test_ai_analysis_does_not_mix_final_action_with_newer_wait_lifecycle(monkeypatch):
    import app.api.routes.analysis as analysis
    import app.api.routes.strategy as strategy

    states = iter([
        {
            "symbol": "NIFTY",
            "active_side": "CALL",
            "candidate_side": "CALL",
            "confirmations": 3,
            "reversal_confirmations": 0,
            "lifecycle": "HOLD_CALL",
            "last_confirmation_at": None,
            "last_evaluation_at": None,
        },
        {
            "symbol": "NIFTY",
            "active_side": "NONE",
            "candidate_side": "NONE",
            "confirmations": 0,
            "reversal_confirmations": 0,
            "lifecycle": "WAIT",
            "last_confirmation_at": None,
            "last_evaluation_at": None,
        },
    ])

    market_data = {
        "timestamp": "2026-10-06T12:11:11+05:30",
        "market_open": True,
        "spot": {"price": 22500},
        "decision": {
            "market_bias": "Bullish",
            "bull_score": 70,
            "bear_score": 30,
            "margin": 40,
            "confidence": 60,
            "preferred_side": "CALL",
            "raw_preferred_side": "CALL",
            "signal_lifecycle": "HOLD_CALL",
            "signal_candidate": "CALL",
            "signal_confirmations": 3,
            "signal_reversal_confirmations": 0,
            "signal_active_side": "CALL",
            "hard_gated": False,
            "reasons": [],
            "recommended_strike": "ATM",
        },
        "option_chain": {
            "expiry": "06OCT2026",
            "all_expiries": ["06OCT2026"],
            "data": [],
        },
        "multi_timeframe": {
            "5min": {"trend": "up"},
            "15min": {"trend": "up"},
            "1hr": {"trend": "up"},
        },
        "technicals": {},
    }

    class FakeAnalyzer:
        async def get_full_market_overview(self, symbol, expiry=None, cache_bust=""):
            assert symbol == "NIFTY"
            return market_data

    async def fake_ai(*args, **kwargs):
        # Deliberately no directional preferred_side in the AI payload.
        # This is representative of the final HTTP result object that can
        # later be lifecycle-overlaid independently of the strategy snapshot.
        return {
            "market_bias": "Bullish",
            "preferred_side": "NONE",
            "confidence": 60,
            "bull_score": 70,
            "bear_score": 30,
            "ai_agrees": True,
            "reason": "test",
            "key_factors": [],
            "timeframe_trend": {},
        }

    load_calls = []

    async def fake_load(symbol):
        assert symbol == "NIFTY"
        load_calls.append(symbol)
        return next(states)

    async def fake_final_action(symbol, data, persisted_state):
        assert symbol == "NIFTY"
        assert persisted_state["lifecycle"] == "HOLD_CALL"
        assert data["decision"]["signal_lifecycle"] == "HOLD_CALL"
        return {
            "best_strategy": "BUY CE",
            "signal_action": "BUY CE",
            "best_score": 61,
            "candidates": {
                "BUY CE": 61,
                "BUY PE": 13,
                "SELL CE": 0,
                "SELL PE": 27,
                "WAIT": 0,
            },
            "lifecycle": "HOLD_CALL",
            "active_side": "CALL",
            "confluence": {},
            "v3_gate": {
                "allowed": True,
                "reason": "Existing confirmed direction ? HOLD is not a fresh entry.",
            },
        }

    async def fake_save(*args, **kwargs):
        return None

    monkeypatch.setattr(analysis, "get_ai_analysis", fake_ai)
    monkeypatch.setattr(analysis, "load_signal_state", fake_load)
    monkeypatch.setattr(analysis, "save_analysis_result", fake_save)
    monkeypatch.setattr(strategy, "resolve_final_strategy_action", fake_final_action)

    result = asyncio.run(
        analysis.ai_analysis(
            "NIFTY",
            analyzer=FakeAnalyzer(),
            ai=object(),
        )
    )

    # Regression invariant:
    # the HTTP result must never expose an actionable strategy together
    # with a newer WAIT lifecycle.
    assert result["signal_lifecycle"] != "WAIT" or result["best_strategy"] == "WAIT"
    assert result["signal_action"] != "BUY CE" or result["signal_lifecycle"] != "WAIT"
    assert load_calls == ["NIFTY"], (
        "Analysis HTTP route must use the same lifecycle snapshot as "
        "build_ai_analysis(); a second DB read can reintroduce action/lifecycle races."
    )
