import asyncio

import pytest


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])
@pytest.mark.asyncio
async def test_paper_capture_scheduler_uses_capture_only_route(monkeypatch, symbol):
    from app.services import history_collector

    captured = {}

    async def fake_wait():
        return None

    async def fake_recommendation(**kwargs):
        captured.update(kwargs)
        return {
            "best_strategy": "WAIT",
            "signal_lifecycle": "WAIT",
            "capture_only": True,
        }

    monkeypatch.setattr(
        history_collector,
        "_wait_for_option_chain_foreground_grace",
        fake_wait,
    )
    monkeypatch.setattr(
        "app.api.routes.strategy.strike_recommendation",
        fake_recommendation,
    )
    monkeypatch.setattr(
        history_collector,
        "set_angel_call_priority",
        lambda priority: "priority-token",
    )
    monkeypatch.setattr(
        history_collector,
        "reset_angel_call_priority",
        lambda token: None,
    )

    result = await history_collector._run_strategy_recommendation_for_capture(
        analyzer=object(),
        symbol=symbol,
    )

    assert result is True
    assert captured["symbol"] == symbol
    assert captured["ai"] is None
    assert captured["_capture_only"] is True


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])
@pytest.mark.asyncio
async def test_capture_only_wait_preserves_lifecycle_without_history_or_ai(monkeypatch, symbol):
    import app.api.routes.strategy as strategy

    class FakeAnalyzer:
        async def get_full_market_overview(
            self, symbol, expiry=None, cache_bust=""
        ):
            return {
                "symbol": symbol,
                "decision": {
                    "preferred_side": "NONE",
                    "raw_preferred_side": "NONE",
                    "margin": 0,
                    "confidence": 50,
                    "signal_strength": 50,
                    "bull_score": 10,
                    "bear_score": 10,
                    "market_bias": "Sideways",
                    "strategy": "WAIT",
                    "risk": "Medium",
                    "hard_gated": False,
                    "market_regime_no_trade": False,
                },
                "spot": {"price": 100.0, "market_open": True},
                "option_chain": {
                    "symbol": symbol,
                    "expiry": "08-Oct-2026",
                    "all_expiries": [],
                },
                "pcr": 0.8,
                "vix": 12.0,
                "technicals": {"adx": 20.0, "atr": 5.0},
                "timestamp": "2026-10-05T12:15:00+05:30",
            }

    saved_states = []

    async def fake_load(symbol):
        assert symbol == symbol
        return None

    def fake_lifecycle(decision, persisted_state, now):
        return (
            dict(
                decision,
                signal_lifecycle="WAIT",
                signal_candidate="NONE",
                signal_confirmations=0,
                signal_reversal_confirmations=0,
                signal_active_side="NONE",
                preferred_side="NONE",
            ),
            {
                "active_side": "NONE",
                "candidate_side": "NONE",
                "confirmations": 0,
                "reversal_confirmations": 0,
                "lifecycle": "WAIT",
                "last_confirmation_at": None,
                "last_evaluation_at": None,
                "margin": 0,
            },
        )

    async def fake_save(state):
        saved_states.append(dict(state))

    async def unexpected_call(*args, **kwargs):
        pytest.fail("capture-only WAIT must not run history or AI work")

    monkeypatch.setattr(strategy, "load_signal_state", fake_load)
    monkeypatch.setattr(
        strategy, "apply_persistent_signal_lifecycle", fake_lifecycle
    )
    monkeypatch.setattr(strategy, "save_signal_state", fake_save)
    monkeypatch.setattr(
        strategy,
        "resolve_final_strategy_action",
        lambda *args, **kwargs: {
            "best_strategy": "WAIT",
            "best_score": 45,
            "candidates": {
                "WAIT": 45,
                "BUY CE": 10,
                "BUY PE": 10,
                "SELL CE": 10,
                "SELL PE": 10,
            },
            "lifecycle": "WAIT",
            "active_side": "NONE",
            "confluence": {},
            "regime": {"no_trade": False, "regime": "RANGE"},
            "v3_gate": {
                "allowed": False,
                "reason": "Signal is not CONFIRMED yet.",
            },
            "iv_info": {},
            "whipsaw_result": {},
        },
    )
    monkeypatch.setattr(
        strategy, "_time_filter",
        lambda: {"session": "MID", "warning": ""},
    )
    monkeypatch.setattr(
        strategy, "_expiry_filter",
        lambda expiry: {"days_left": 3, "warning": ""},
    )
    monkeypatch.setattr(strategy, "_classify_market_state", lambda data: "RANGE")
    monkeypatch.setattr(strategy, "record_signal_persistent", unexpected_call)
    monkeypatch.setattr(strategy, "get_history_persistent", unexpected_call)
    monkeypatch.setattr(
        strategy, "_persist_analysis_history_if_due", unexpected_call
    )
    monkeypatch.setattr(strategy, "get_ai_analysis", unexpected_call)

    result = await strategy.strike_recommendation(
        symbol=symbol,
        analyzer=FakeAnalyzer(),
        ai=None,
        _capture_only=True,
    )

    assert result["capture_only"] is True
    assert result["best_strategy"] == "WAIT"
    assert result["signal_action"] == "WAIT"
    assert result["signal_lifecycle"] == "WAIT"
    assert len(saved_states) == 2
    assert saved_states[-1]["strategy"] == "WAIT"
    assert saved_states[-1]["strategy_score"] == 45
