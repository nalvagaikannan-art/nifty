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


def _approved_analysis_authority(action="BUY CE"):
    from app.services.entry_risk_authority import build_entry_risk_authority

    lot_size = 65
    entry = 100.0
    stop = 80.0
    risk_per_lot = (entry - stop) * lot_size
    return build_entry_risk_authority(
        action=action,
        risk_assessment={
            "allowed": True,
            "lots": 1,
            "quantity": lot_size,
            "max_loss": risk_per_lot,
            "max_risk_amount": risk_per_lot,
            "risk_per_lot": risk_per_lot,
            "no_trade_reasons": [],
            "reasons": ["Controlled test approval"],
            "warnings": [],
        },
        selected_contract={
            "symbol": "NIFTY",
            "strike": 25000,
            "type": action.split()[1],
            "expiry": "15DEC2026",
            "entry_price": entry,
            "sl": stop,
        },
        trade_levels={
            "option_entry": entry,
            "option_sl": stop,
            "option_rr_ratio": 2.0,
        },
        lot_size=lot_size,
    )


def test_analysis_finalizer_rejects_missing_risk_authority():
    from app.api.routes.analysis import _finalize_ai_analysis_result

    result = {
        "signal_action": "BUY CE",
        "best_strategy": "BUY CE",
        "signal_lifecycle": "CONFIRMED_CALL",
        "fresh_entry_allowed": True,
        "hard_gated": False,
        "market_regime": {"regime": "TREND_UP", "no_trade": False},
        "v3_gate": {"allowed": True, "fresh_entry_allowed": True},
        "recommended_option": {"available": True, "strike": 25000},
        "recommended_strike": "ATM",
        "price_levels": {"entry": 100},
        "strategy_detail": {"name": "Directional Call"},
    }

    result = _finalize_ai_analysis_result(result)

    assert result["fresh_entry_allowed"] is False
    assert result["recommended_option"] is None
    assert result["recommended_strike"] == "NONE"
    assert result["price_levels"] is None
    assert result["strategy_detail"] is None


def test_analysis_finalizer_accepts_only_matching_approved_buy_authority():
    from app.api.routes.analysis import _finalize_ai_analysis_result

    result = {
        "signal_action": "BUY CE",
        "best_strategy": "BUY CE",
        "signal_lifecycle": "CONFIRMED_CALL",
        "fresh_entry_allowed": True,
        "hard_gated": False,
        "market_regime": {"regime": "TREND_UP", "no_trade": False},
        "v3_gate": {"allowed": True, "fresh_entry_allowed": True},
        "entry_risk_authority": _approved_analysis_authority("BUY CE"),
        "recommended_option": {"available": True, "strike": 25000},
    }

    result = _finalize_ai_analysis_result(result)

    assert result["fresh_entry_allowed"] is True
    assert result["recommended_option"]["strike"] == 25000


def test_analysis_finalizer_rejects_authority_action_mismatch():
    from app.api.routes.analysis import _finalize_ai_analysis_result

    result = {
        "signal_action": "BUY PE",
        "best_strategy": "BUY PE",
        "signal_lifecycle": "CONFIRMED_PUT",
        "fresh_entry_allowed": True,
        "hard_gated": False,
        "market_regime": {"regime": "TREND_DOWN", "no_trade": False},
        "v3_gate": {"allowed": True, "fresh_entry_allowed": True},
        "entry_risk_authority": _approved_analysis_authority("BUY CE"),
        "recommended_option": {"available": True, "strike": 25000},
    }

    result = _finalize_ai_analysis_result(result)

    assert result["fresh_entry_allowed"] is False
    assert result["recommended_option"] is None


def test_analysis_shared_risk_uses_canonical_levels_and_liquidity(monkeypatch):
    import asyncio
    import app.api.routes.analysis as analysis
    import app.services.contract_specs as contract_specs
    import app.services.trade_levels as trade_levels_module
    import app.utils.helpers as helpers
    import app.services.entry_risk_service as risk_service
    from app.services.entry_risk_authority import (
        build_entry_risk_authority,
        is_entry_risk_approved,
    )

    calls = {}

    monkeypatch.setattr(
        contract_specs, "resolve_lot_size", lambda symbol, chain: 65
    )
    monkeypatch.setattr(
        helpers, "intraday_hold_days_to_close", lambda timestamp: 0.25
    )

    def fake_levels(md, direction, spot, premium, **kwargs):
        calls["levels"] = {
            "direction": direction,
            "spot": spot,
            "premium": premium,
            **kwargs,
        }
        return {
            "option_entry": 100.0,
            "option_sl": 80.0,
            "option_t1": 120.0,
            "option_t2": 140.0,
            "option_t3": 150.0,
            "rr_ratio": 2.0,
            "option_rr_ratio": 2.0,
        }

    monkeypatch.setattr(
        trade_levels_module, "calculate_trade_levels", fake_levels
    )

    async def fake_service(**kwargs):
        calls["service"] = kwargs
        contract = kwargs["selected_contract"]
        levels = kwargs["trade_levels"]
        size = kwargs["lot_size"]
        risk_per_lot = (
            levels["option_entry"] - levels["option_sl"]
        ) * size
        verdict = {
            "allowed": True,
            "lots": 1,
            "quantity": size,
            "max_loss": risk_per_lot,
            "max_risk_amount": risk_per_lot,
            "risk_per_lot": risk_per_lot,
            "no_trade_reasons": [],
            "reasons": ["Controlled service approval"],
            "warnings": [],
        }
        authority = build_entry_risk_authority(
            action=kwargs["action"],
            risk_assessment=verdict,
            selected_contract=contract,
            trade_levels=levels,
            lot_size=size,
        )
        return {
            "risk_assessment": verdict,
            "entry_risk_authority": authority,
            "risk_approved": is_entry_risk_approved(authority),
        }

    monkeypatch.setattr(
        risk_service, "assess_entry_risk_authority", fake_service
    )

    result = {
        "symbol": "NIFTY",
        "signal_action": "BUY CE",
        "best_strategy": "BUY CE",
        "fresh_entry_allowed": True,
        "signal_lifecycle": "CONFIRMED_CALL",
        "hard_gated": False,
        "signal_strength": 70,
        "confluence": {"quality": "HIGH"},
        "market_regime": {"regime": "TREND_UP", "no_trade": False},
        "v3_gate": {"allowed": True, "fresh_entry_allowed": True},
        "recommended_option": {
            "available": True,
            "symbol": "NIFTY",
            "strike": 25000,
            "type": "CE",
            "expiry": "15DEC2026",
            "entry_price": 101.0,
            "sl": 81.0,
            "delta": 0.5,
            "theta_per_day": -2.0,
            "oi": 1200,
            "volume": 2400,
        },
    }
    market_data = {
        "symbol": "NIFTY",
        "timestamp": "2026-11-06T10:00:00+05:30",
        "spot": {"price": 25000.0},
        "option_chain": {"symbol": "NIFTY", "expiry": "15DEC2026"},
        "vix": 14.0,
    }

    result = asyncio.run(
        analysis._apply_entry_risk_authority(result, market_data)
    )

    assert result["fresh_entry_allowed"] is True
    assert result["entry_risk_authority"]["status"] == "APPROVED"
    assert result["v2_risk"]["allowed"] is True
    assert result["v2_trade_levels"]["option_entry"] == 100.0
    assert result["recommended_option"]["entry_price"] == 100.0
    assert calls["service"]["selected_contract"]["symbol"] == "NIFTY"
    assert calls["service"]["selected_contract"]["oi"] == 1200
    assert calls["service"]["selected_contract"]["volume"] == 2400
    assert calls["service"]["trade_levels"]["option_sl"] == 80.0
    assert calls["levels"]["direction"] == "bullish"
    assert calls["levels"]["lot_size"] == 65


def test_analysis_sell_candidate_cannot_use_buy_risk_authority():
    import asyncio
    import app.api.routes.analysis as analysis

    result = {
        "symbol": "NIFTY",
        "signal_action": "SELL CE",
        "best_strategy": "SELL CE",
        "fresh_entry_allowed": True,
        "signal_lifecycle": "CONFIRMED_PUT",
        "hard_gated": False,
        "signal_strength": 70,
        "confluence": {"quality": "HIGH"},
        "market_regime": {"regime": "TREND_DOWN", "no_trade": False},
        "v3_gate": {"allowed": True, "fresh_entry_allowed": True},
        "recommended_option": {
            "available": True,
            "strike": 25000,
            "type": "CE",
            "expiry": "15DEC2026",
            "entry_price": 100.0,
            "oi": 1200,
            "volume": 2400,
        },
    }

    result = asyncio.run(
        analysis._apply_entry_risk_authority(
            result,
            {
                "symbol": "NIFTY",
                "spot": {"price": 25000.0},
                "option_chain": {"expiry": "15DEC2026"},
            },
        )
    )

    assert result["fresh_entry_allowed"] is False
    assert result["entry_risk_authority"]["status"] == "UNASSESSED"
    assert result["signal_action"] == "WAIT"
    assert result["v2_risk"] is None
    assert result["v2_trade_levels"] is None


def test_analysis_dashboard_requires_matching_risk_authority():
    from pathlib import Path

    source = Path("app/templates/analysis.html").read_text()
    assert "function analysisRiskApproved(d)" in source
    assert source.count("analysisRiskApproved(d)") >= 4

def test_analysis_recommendation_preserves_chain_symbol(monkeypatch):
    import app.api.routes.analysis as analysis
    import app.api.routes.strategy as strategy
    import app.utils.helpers as helpers

    monkeypatch.setattr(
        helpers, "intraday_hold_days_to_close", lambda timestamp: 0.25
    )
    monkeypatch.setattr(
        strategy,
        "_pick_strikes",
        lambda chain_data, *args, **kwargs: [{
            "symbol": chain_data.get("symbol"),
            "strike": 50000,
            "type": "CE",
            "expiry": chain_data.get("expiry"),
            "entry_price": 100.0,
            "ltp": 101.0,
        }],
    )

    market_data = {
        "symbol": "BANKNIFTY",
        "timestamp": "2026-11-06T10:00:00+05:30",
        "spot": {"price": 50000.0},
        "technicals": {"atr": 100.0},
        "option_chain": {
            "symbol": "BANKNIFTY",
            "expiry": "15DEC2026",
            "data": [{"strikePrice": 50000}],
        },
    }

    recommendation = analysis._pick_recommended_option(
        market_data,
        {},
        "BUY CE",
    )

    assert recommendation["available"] is True
    assert recommendation["symbol"] == "BANKNIFTY"
