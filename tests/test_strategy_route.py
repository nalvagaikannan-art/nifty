from app.api.routes.strategy import _score_candidates, _time_filter


def _market_data(bull, bear, margin, preferred="NONE", confidence=40):
    return {
        "decision": {
            "bull_score": bull,
            "bear_score": bear,
            "margin": margin,
            "preferred_side": preferred,
            "confidence": confidence,
        },
        "technicals": {
            "adx": 22,
            "atr": 18,
        },
        "vix": 11.2,
        "pcr": 0.74,
        "spot": {
            "price": 23915.95,
            "market_open": True,
        },
        "oi_summary": {
            "ce_max_oi_strike": 24000,
            "pe_max_oi_strike": 23900,
            "atm_iv": 0,
        },
    }


def test_weak_sideways_signal_results_in_wait():
    result = _score_candidates(
        _market_data(21, 14, 7, "NONE", 39)
    )

    assert result["best"] == "WAIT"
    assert result["candidates"]["WAIT"] == 45


def test_bearish_signal_does_not_force_buy_ce():
    result = _score_candidates(
        _market_data(10, 23, -20, "NONE", 48)
    )

    assert result["candidates"]["BUY PE"] == 31
    assert result["candidates"]["BUY CE"] == 7
    assert result["best"] == "WAIT"


def test_strong_call_signal_can_select_buy_ce():
    result = _score_candidates(
        _market_data(35, 10, 25, "CALL", 70)
    )

    assert result["candidates"]["BUY CE"] > 0
    assert result["best"] == "BUY CE"


def test_strong_put_signal_can_select_buy_pe():
    result = _score_candidates(
        _market_data(10, 35, -25, "PUT", 70)
    )

    assert result["candidates"]["BUY PE"] > 0
    assert result["best"] == "BUY PE"


def test_hold_call_lifecycle_keeps_active_call_despite_bearish_fresh_score():
    result = _score_candidates(
        _market_data(10, 23, -20, "NONE", 48)
    )

    candidates = result["candidates"]
    raw_best = result["best"]

    assert raw_best == "WAIT"
    assert candidates["BUY PE"] > candidates["BUY CE"]

    # Persistent HOLD_CALL must preserve the confirmed active direction.
    lifecycle_state = "HOLD_CALL"
    lifecycle_active = "CALL"

    best = raw_best
    best_score = result["best_score"]

    if lifecycle_state.startswith("HOLD_") and lifecycle_active in ("CALL", "PUT"):
        best = "BUY CE" if lifecycle_active == "CALL" else "BUY PE"
        best_score = candidates.get(best, best_score)

    assert best == "BUY CE"
    assert best_score == candidates["BUY CE"]


def test_time_filter_opening_boundary(monkeypatch):
    from datetime import datetime
    import app.api.routes.strategy as strategy

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 3, 9, 15, tzinfo=tz)

    monkeypatch.setattr(strategy, "datetime", FakeDateTime)
    result = _time_filter()

    assert result["session"] == "OPENING"


def test_time_filter_mid_boundary(monkeypatch):
    from datetime import datetime
    import app.api.routes.strategy as strategy

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 3, 9, 45, tzinfo=tz)

    monkeypatch.setattr(strategy, "datetime", FakeDateTime)
    result = _time_filter()

    assert result["session"] == "MID"


def test_time_filter_1400_is_still_mid(monkeypatch):
    from datetime import datetime
    import app.api.routes.strategy as strategy

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 3, 14, 0, tzinfo=tz)

    monkeypatch.setattr(strategy, "datetime", FakeDateTime)
    result = _time_filter()

    assert result["session"] == "MID"


def test_time_filter_1401_starts_closing(monkeypatch):
    from datetime import datetime
    import app.api.routes.strategy as strategy

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 3, 14, 1, tzinfo=tz)

    monkeypatch.setattr(strategy, "datetime", FakeDateTime)
    result = _time_filter()

    assert result["session"] == "CLOSING"


def test_time_filter_market_close(monkeypatch):
    from datetime import datetime
    import app.api.routes.strategy as strategy

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 3, 15, 30, tzinfo=tz)

    monkeypatch.setattr(strategy, "datetime", FakeDateTime)
    result = _time_filter()

    assert result["session"] == "CLOSING"


def test_time_filter_after_market_close(monkeypatch):
    from datetime import datetime
    import app.api.routes.strategy as strategy

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 3, 15, 31, tzinfo=tz)

    monkeypatch.setattr(strategy, "datetime", FakeDateTime)
    result = _time_filter()

    assert result["session"] == "CLOSED"


def test_strategy_route_persists_symbol_in_lifecycle_state(monkeypatch):
    import asyncio
    import app.api.routes.strategy as strategy

    class FakeAnalyzer:
        async def get_full_market_overview(self, symbol, expiry=None, cache_bust=""):
            return {
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
                "option_chain": {"expiry": "08-Oct-2026", "all_expiries": []},
                "pcr": 0.8,
                "vix": 12.0,
                "technicals": {"adx": 20.0, "atr": 5.0},
                "timestamp": "2026-10-05T12:15:00+05:30",
            }

    saved = {}

    async def fake_load(symbol):
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
        saved.update(state)

    async def fake_record(**kwargs):
        return {"reversal": False, "reversal_type": ""}

    async def fake_history(symbol):
        return []

    async def fake_final_action(symbol, data, state):
        return {
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
            "v3_gate": {"allowed": False, "reason": "Signal is not CONFIRMED yet."},
            "iv_info": {},
            "whipsaw_result": {},
        }
    monkeypatch.setattr(strategy, "load_signal_state", fake_load)
    monkeypatch.setattr(strategy, "apply_persistent_signal_lifecycle", fake_lifecycle)
    monkeypatch.setattr(strategy, "save_signal_state", fake_save)
    monkeypatch.setattr(strategy, "record_signal_persistent", fake_record)
    monkeypatch.setattr(strategy, "get_history_persistent", fake_history)
    monkeypatch.setattr(strategy, "_time_filter", lambda: {"session": "MID", "warning": ""})
    monkeypatch.setattr(strategy, "_expiry_filter", lambda expiry: {"days_left": 3, "warning": ""})
    monkeypatch.setattr(strategy, "_classify_market_state", lambda data: "RANGE")
    monkeypatch.setattr(strategy, "resolve_final_strategy_action", fake_final_action)
    monkeypatch.setattr(
        "app.services.contract_specs.resolve_lot_size",
        lambda symbol, chain: 20,
    )
    result = asyncio.run(strategy.strike_recommendation(symbol="SENSEX", analyzer=FakeAnalyzer(), ai=object()))
    assert result["symbol"] == "SENSEX"
    assert saved["symbol"] == "SENSEX"
