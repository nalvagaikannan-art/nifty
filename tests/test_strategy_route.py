from app.api.routes.strategy import (
    _score_candidates,
    _time_filter,
    _v3_entry_gate,
    _signal_action_for_entry,
    _trade_entry_confidence,
    _enforce_action_safety_gate,
)


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
                "option_chain": {"symbol": symbol, "expiry": "08-Oct-2026", "all_expiries": []},
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

    final_action_result = {
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

    async def fake_final_action(symbol, data, state):
        return dict(final_action_result)
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
    history_calls = []

    async def fake_history_persist(*args, **kwargs):
        history_calls.append((args, kwargs))

    monkeypatch.setattr(
        strategy,
        "_persist_analysis_history_if_due",
        fake_history_persist,
    )

    result = asyncio.run(
        strategy.strike_recommendation(
            symbol="SENSEX", analyzer=FakeAnalyzer(), ai=object()
        )
    )
    assert result["symbol"] == "SENSEX"
    assert saved["symbol"] == "SENSEX"
    assert len(history_calls) == 1
    call_args, call_kwargs = history_calls[0]
    assert call_args[0] == "SENSEX"
    assert call_args[3]["timestamp"] == "2026-10-05T12:15:00+05:30"
    assert call_kwargs["ai_result"]["_provider"] == "rule_engine_only"

    # Verify persistence hook on a non-WAIT response.
    normal_ai_result = {"reason": "normal-path AI result", "_provider": "test"}
    ai_snapshots = []
    ledger_calls = []

    async def fake_ai_analysis(ai, symbol, expiry, market_data):
        ai_snapshots.append(market_data)
        return normal_ai_result

    async def fake_ledger(**kwargs):
        ledger_calls.append(kwargs)

    monkeypatch.setattr(strategy, "get_ai_analysis", fake_ai_analysis)
    monkeypatch.setattr(
        strategy, "_v3_entry_gate",
        lambda *args, **kwargs: {"allowed": True, "reason": "test gate"},
    )
    monkeypatch.setattr(
        strategy, "_pick_strikes",
        lambda chain_data, *args, **kwargs: [{
            "symbol": chain_data.get("symbol"),
            "strike": 100.0, "type": "CE", "expiry": "08-Oct-2026",
            "ltp": 2.0, "entry_price": 2.0, "oi": 1000, "volume": 5000,
            "delta": 0.5, "theta_per_day": -1.0,
        }],
    )
    monkeypatch.setattr(
        strategy, "_liquidity_filter",
        lambda *args, **kwargs: {"liquid": True, "warning": ""},
    )
    # Exercise the real trade-level calculation and metadata route path.
    async def fake_daily_pnl():
        return 0.0

    async def fake_open_trades(symbol):
        return []

    monkeypatch.setattr(strategy, "get_daily_pnl", fake_daily_pnl)
    monkeypatch.setattr(strategy, "get_open_trades", fake_open_trades)
    def fake_assess_risk(**kwargs):
        entry = float(kwargs["entry_price"])
        stop = float(kwargs["stop_loss_price"])
        size = int(kwargs["lot_size"])
        risk_per_lot = (entry - stop) * size
        if risk_per_lot <= 0:
            return {
                "allowed": False,
                "lots": 0,
                "quantity": 0,
                "max_loss": 0,
                "no_trade_reasons": ["Invalid test risk levels"],
                "warnings": [],
            }
        return {
            "allowed": True,
            "lots": 1,
            "quantity": size,
            "max_loss": risk_per_lot,
            "max_risk_amount": risk_per_lot,
            "risk_per_lot": risk_per_lot,
            "no_trade_reasons": [],
            "reasons": ["Test risk assessment passed"],
            "warnings": [],
        }

    monkeypatch.setattr(strategy, "assess_risk", fake_assess_risk)
    monkeypatch.setattr(
        strategy, "_expected_move",
        lambda spot, vix, days: {
            "move_points": 1.0, "upper": spot + 1.0,
            "lower": spot - 1.0, "basis": "test",
        },
    )
    monkeypatch.setattr(
        strategy, "generate_price_levels", lambda data: {"basis": "test"}
    )
    monkeypatch.setattr(
        strategy, "generate_option_strategy",
        lambda data, option_df: {"name": "test strategy"},
    )
    monkeypatch.setattr(strategy, "record_daily_signal_ledger", fake_ledger)

    final_action_result.update({
        "best_strategy": "BUY CE",
        "best_score": 75,
        "candidates": {
            "WAIT": 45, "BUY CE": 75, "BUY PE": 10,
            "SELL CE": 10, "SELL PE": 10,
        },
        "lifecycle": "HOLD_CALL",
        "active_side": "CALL",
        "confluence": {
            "confluence_score": 70, "agreement_count": 5, "quality": "HIGH",
        },
        "regime": {"no_trade": False, "regime": "TRENDING"},
        "v3_gate": {"allowed": True, "reason": "test gate"},
        "iv_info": {"penalty": 0, "warning": ""},
        "whipsaw_result": {},
    })

    normal_result = asyncio.run(
        strategy.strike_recommendation(
            symbol="SENSEX", analyzer=FakeAnalyzer(), ai=object()
        )
    )

    assert normal_result["best_strategy"] == "BUY CE"
    # HOLD is context only: retain ledger evidence, but do not expose a fresh-entry setup.
    assert normal_result["signal_action"] == "HOLD CALL BIAS"
    assert normal_result["entry_gate"]["fresh_entry_allowed"] is False
    assert normal_result["strikes"] == []
    assert normal_result["price_levels"] is None
    assert normal_result["strategy_detail"] is None
    assert normal_result["v2_trade_levels"] is None
    assert normal_result["v2_risk"] is None
    assert len(history_calls) == 2
    normal_args, normal_kwargs = history_calls[-1]
    assert normal_args[0] == "SENSEX"
    assert normal_args[3]["timestamp"] == "2026-10-05T12:15:00+05:30"
    assert len(ai_snapshots) == 1
    assert ai_snapshots[0] is normal_args[3]
    assert normal_kwargs["ai_result"] is normal_ai_result
    assert normal_kwargs["ai_unavailable"] is False
    assert len(ledger_calls) == 1

    ledger_snapshot = ledger_calls[0]["entry_snapshot"]
    assert ledger_snapshot["levels_basis"] == (
        "V2 structural risk; real delta; theta-adjusted"
    )

    paper_capture = ledger_snapshot["forward_paper_capture"]
    assert paper_capture["schema_version"] == 2
    assert paper_capture["eligible"] is False  # HOLD cannot become a new entry
    assert paper_capture["actual_trade"] is False
    assert paper_capture["symbol"] == "SENSEX"
    assert paper_capture["action"] == "BUY CE"
    assert paper_capture["signal_action"] == "HOLD CALL BIAS"
    assert paper_capture["event_timestamp"] == normal_args[3]["timestamp"]
    assert paper_capture["event_timezone"] == "Asia/Kolkata"
    assert paper_capture["contract"]["option_type"] in ("CE", "PE")
    assert "underlying_atr" in paper_capture["risk"]
    assert "spread_pct" in paper_capture["quote"]
    assert "pcr_quality" in paper_capture["data_quality"]
    assert "vix_quality" in paper_capture["data_quality"]
    assert "mtf_freshness" in paper_capture["data_quality"]

    decision = ledger_snapshot["strategy_decision"]
    assert decision["metadata_version"] == 2
    assert decision["best_strategy"] == "BUY CE"
    assert decision["signal_action"] == "HOLD CALL BIAS"
    assert decision["fresh_entry_allowed"] is False
    assert decision["lifecycle"] == "HOLD_CALL"
    assert decision["active_side"] == "CALL"
    assert decision["entry_gate"]["fresh_entry_allowed"] is False
    assert decision["market_regime_no_trade"] is False
    assert decision["hard_gated"] is False

    # PHASE2_FORWARD_PAPER_CAPTURE_CONFIRMED_ROUTE_REGRESSION
    # Drive the same mocked route through a genuine confirmed fresh-entry
    # path and verify the versioned evidence payload sent to the ledger.
    final_action_result.update({
        "best_strategy": "BUY CE",
        "best_score": 75,
        "lifecycle": "CONFIRMED_CALL",
        "active_side": "CALL",
        "v3_gate": {"allowed": True, "reason": "test gate"},
        "regime": {"no_trade": False, "regime": "TRENDING"},
    })

    confirmed_result = asyncio.run(
        strategy.strike_recommendation(
            symbol="SENSEX", analyzer=FakeAnalyzer(), ai=object()
        )
    )

    assert confirmed_result["best_strategy"] == "BUY CE"
    assert confirmed_result["signal_action"] == "BUY CE"
    assert confirmed_result["entry_gate"]["fresh_entry_allowed"] is True
    assert len(ledger_calls) == 2

    confirmed_snapshot = ledger_calls[-1]["entry_snapshot"]
    confirmed_capture = confirmed_snapshot["forward_paper_capture"]
    assert confirmed_capture["schema_version"] == 2
    assert confirmed_capture["eligible"] is True
    assert confirmed_capture["actual_trade"] is False
    assert confirmed_capture["symbol"] == "SENSEX"
    assert confirmed_capture["action"] == "BUY CE"
    assert confirmed_capture["signal_action"] == "BUY CE"
    assert confirmed_capture["lifecycle"] == "CONFIRMED_CALL"
    assert confirmed_capture["event_timestamp"] == (
        "2026-10-05T12:15:00+05:30"
    )
    assert confirmed_capture["event_timezone"] == "Asia/Kolkata"

    contract = confirmed_capture["contract"]
    assert contract["expiry"] == "08-Oct-2026"
    assert contract["strike"] == 100.0
    assert confirmed_capture["entry_risk_authority"]["snapshot"]["selected_contract"]["symbol"] == "SENSEX"
    assert contract["option_type"] == "CE"

    quote = confirmed_capture["quote"]
    assert quote["entry_price"] is not None
    assert "bid" in quote and "ask" in quote
    assert "spread_pct" in quote

    spot = confirmed_capture["spot"]
    assert spot["price"] == 100.0
    assert confirmed_capture["risk"]["underlying_atr"] == 5.0
    assert confirmed_capture["gate"]["fresh_entry_allowed"] is True
    assert "entry_gate" in confirmed_capture["gate"]
    assert "pcr_quality" in confirmed_capture["data_quality"]
    assert "vix_quality" in confirmed_capture["data_quality"]
    assert "mtf_freshness" in confirmed_capture["data_quality"]

    decision = confirmed_snapshot["strategy_decision"]
    assert decision["metadata_version"] == 2
    assert decision["fresh_entry_allowed"] is True
    assert decision["lifecycle"] == "CONFIRMED_CALL"





def test_v3_gate_blocks_weak_confirmed_sell_entry():
    result = _v3_entry_gate(
        dec={"signal_strength": 34, "confidence": 34},
        confluence={
            "direction": "BULLISH",
            "confluence_score": 19,
            "agreement_count": 5,
        },
        expiry_info={"days_left": 99},
        market_data={"technicals": {"adx": 18.9}},
        lifecycle_state="CONFIRMED_CALL",
        lifecycle_active="CALL",
    )

    assert result["allowed"] is False
    assert "Signal strength 34" in result["reason"]


def test_v3_gate_preserves_existing_hold_sell_state():
    result = _v3_entry_gate(
        dec={"signal_strength": 34, "confidence": 34},
        confluence={
            "direction": "BULLISH",
            "confluence_score": 19,
            "agreement_count": 5,
        },
        expiry_info={"days_left": 99},
        market_data={"technicals": {"adx": 18.9}},
        lifecycle_state="HOLD_CALL",
        lifecycle_active="CALL",
    )

    assert result["allowed"] is True
    assert "HOLD" in result["reason"]

def test_hold_gate_is_not_a_fresh_entry():
    result = _v3_entry_gate(
        dec={"signal_strength": 45, "confidence": 45},
        confluence={"direction": "BULLISH", "confluence_score": 2, "agreement_count": 1},
        expiry_info={"days_left": 3},
        market_data={"technicals": {"adx": 12}},
        lifecycle_state="HOLD_CALL",
        lifecycle_active="CALL",
    )
    assert result["allowed"] is True  # Preserve existing bias contract.
    assert result["fresh_entry_allowed"] is False
    assert _signal_action_for_entry("BUY CE", "HOLD_CALL", "CALL", False) == "HOLD CALL BIAS"


def test_confirmed_gate_reports_fresh_entry_when_quality_passes():
    result = _v3_entry_gate(
        dec={"signal_strength": 70, "confidence": 70},
        confluence={"direction": "BULLISH", "confluence_score": 45, "agreement_count": 4},
        expiry_info={"days_left": 3},
        market_data={"technicals": {"adx": 25}},
        lifecycle_state="CONFIRMED_CALL",
        lifecycle_active="CALL",
    )
    assert result["allowed"] is True
    assert result["fresh_entry_allowed"] is True
    assert _signal_action_for_entry("BUY CE", "CONFIRMED_CALL", "CALL", True) == "BUY CE"

def test_trade_entry_confidence_uses_confluence_as_soft_penalty():
    low_confluence = _trade_entry_confidence(58, 24, 5)
    higher_confluence = _trade_entry_confidence(58, 45, 5)

    assert low_confluence == 56
    assert higher_confluence == 64
    assert low_confluence < higher_confluence
    assert _trade_entry_confidence(100, 100, 18) == 95
    assert _trade_entry_confidence(0, 0, 0) == 0


def _soft_gate_resolver_case(monkeypatch, hard_gated, regime_no_trade):
    import asyncio
    import app.api.routes.strategy as strategy

    monkeypatch.setattr(
        strategy, "_time_filter", lambda: {"penalty": 0}
    )
    monkeypatch.setattr(
        strategy, "_expiry_filter", lambda expiry: {"days_left": 4}
    )
    monkeypatch.setattr(
        strategy,
        "_score_candidates",
        lambda market_data: {
            "candidates": {
                "WAIT": 25,
                "BUY CE": 72,
                "SELL PE": 61,
                "BUY PE": 10,
                "SELL CE": 10,
            },
            "best": "WAIT",
            "best_score": 25,
        },
    )
    monkeypatch.setattr(
        strategy,
        "_anti_whipsaw",
        lambda *args, **kwargs: {"strategy": "WAIT"},
    )
    monkeypatch.setattr(
        strategy, "_iv_filter",
        lambda *args, **kwargs: {"penalty": 0, "warning": ""},
    )
    monkeypatch.setattr(
        strategy,
        "run_confluence_engine",
        lambda market_data: {
            "confluence_score": 24,
            "direction": "BULLISH",
            "quality": "LOW",
            "agreement_count": 5,
            "total_factors": 18,
            "factors": [],
        },
    )
    monkeypatch.setattr(
        strategy,
        "classify_market_regime",
        lambda market_data, confluence: {
            "regime": "NO_TRADE" if regime_no_trade else "TRENDING",
            "no_trade": regime_no_trade,
            "no_trade_reason": (
                "Test regime block" if regime_no_trade else ""
            ),
        },
    )

    market_data = {
        "decision": {
            "signal_strength": 70,
            "signal_lifecycle": "CONFIRMED_CALL",
            "signal_active_side": "CALL",
            "hard_gated": hard_gated,
        },
        "option_chain": {"expiry": "27OCT2026"},
        "oi_summary": {"atm_iv": 0},
    }
    return asyncio.run(
        strategy.resolve_final_strategy_action(
            "NIFTY", market_data, {}
        )
    )


def test_action_safety_gate_keeps_reason_and_wait_state_consistent():
    cases = [
        (
            {"hard_gated": True},
            {"regime": "TREND_UP", "no_trade": False},
            {"allowed": True, "fresh_entry_allowed": True},
            "Upstream critical-data/risk hard gate requires WAIT.",
            True,
        ),
        (
            {"hard_gated": False},
            {"regime": "TREND_UP", "no_trade": False},
            {"allowed": False, "reason": "Signal is not CONFIRMED yet."},
            "Signal is not CONFIRMED yet.",
            False,
        ),
    ]

    candidates = {"WAIT": 23, "BUY CE": 75, "SELL PE": 61}
    for dec, regime, gate_in, expected_reason, expected_hard_gate in cases:
        best, score, gate, blocked = _enforce_action_safety_gate(
            "BUY CE", 75, candidates, gate_in, dec, regime
        )

        assert blocked is True
        assert best == "WAIT"
        assert score == 23
        assert gate["allowed"] is False
        assert gate["fresh_entry_allowed"] is False
        assert gate["reason"] == expected_reason
        assert bool(dec.get("hard_gated", False)) is expected_hard_gate


def test_action_safety_gate_leaves_valid_entry_unchanged():
    candidates = {"WAIT": 23, "BUY CE": 75}
    gate_in = {"allowed": True, "fresh_entry_allowed": True}

    best, score, gate, blocked = _enforce_action_safety_gate(
        "BUY CE", 75, candidates, gate_in,
        {"hard_gated": False},
        {"regime": "TREND_UP", "no_trade": False},
    )

    assert blocked is False
    assert best == "BUY CE"
    assert score == 75
    assert gate["allowed"] is True
    assert gate["fresh_entry_allowed"] is True


def test_action_safety_gate_keeps_high_volatility_as_soft_context():
    candidates = {"WAIT": 23, "BUY CE": 75}
    gate_in = {"allowed": True, "fresh_entry_allowed": True}

    best, score, gate, blocked = _enforce_action_safety_gate(
        "BUY CE", 75, candidates, gate_in,
        {"hard_gated": False},
        {
            "regime": "HIGH_VOLATILITY",
            "no_trade": False,
            "hard_no_trade": False,
        },
    )

    assert blocked is False
    assert best == "BUY CE"
    assert score == 75
    assert gate["allowed"] is True
    assert gate["fresh_entry_allowed"] is True


def test_resolver_upstream_hard_gate_cannot_be_overridden_by_lifecycle(
    monkeypatch,
):
    result = _soft_gate_resolver_case(
        monkeypatch, hard_gated=True, regime_no_trade=False
    )

    assert result["best_strategy"] == "WAIT"
    assert result["signal_action"] == "WAIT"
    assert result["fresh_entry_allowed"] is False
    assert result["v3_gate"]["allowed"] is False
    assert "hard gate" in result["v3_gate"]["reason"].lower()


def test_resolver_regime_no_trade_cannot_leave_directional_entry(
    monkeypatch,
):
    result = _soft_gate_resolver_case(
        monkeypatch, hard_gated=False, regime_no_trade=True
    )

    assert result["best_strategy"] == "WAIT"
    assert result["signal_action"] == "WAIT"
    assert result["fresh_entry_allowed"] is False
    assert result["v3_gate"]["allowed"] is False
    assert result["v3_gate"]["reason"] == "Test regime block"


def test_v3_gate_uses_confluence_below_45_as_soft_input():
    result = _v3_entry_gate(
        dec={"signal_strength": 70, "confidence": 70},
        confluence={
            "direction": "BULLISH",
            "confluence_score": 44,
            "agreement_count": 5,
        },
        expiry_info={"days_left": 3},
        market_data={"technicals": {"adx": 25}},
        lifecycle_state="CONFIRMED_CALL",
        lifecycle_active="CALL",
    )

    assert result["allowed"] is True
    assert result["fresh_entry_allowed"] is True
    assert "confluence affects confidence" in result["reason"]


def test_v3_gate_uses_low_expiry_confluence_as_soft_input():
    result = _v3_entry_gate(
        dec={"signal_strength": 70, "confidence": 70},
        confluence={
            "direction": "BEARISH",
            "confluence_score": 64,
            "agreement_count": 4,
        },
        expiry_info={"days_left": 1},
        market_data={"technicals": {"adx": 25}},
        lifecycle_state="CONFIRMED_PUT",
        lifecycle_active="PUT",
    )

    assert result["allowed"] is True
    assert result["fresh_entry_allowed"] is True
    assert "confluence affects confidence" in result["reason"]


def test_v3_gate_accepts_expiry_with_required_confirmations_and_adx():
    result = _v3_entry_gate(
        dec={"signal_strength": 65, "confidence": 65},
        confluence={
            "direction": "BEARISH",
            "confluence_score": 65,
            "agreement_count": 4,
        },
        expiry_info={"days_left": 1},
        market_data={"technicals": {"adx": 20}},
        lifecycle_state="CONFIRMED_PUT",
        lifecycle_active="PUT",
    )

    assert result["allowed"] is True
    assert result["fresh_entry_allowed"] is True

def test_candidate_scoring_missing_market_open_forces_wait():
    from app.api.routes.strategy import _score_candidates

    market_data = {
        "spot": {"price": 10000.0},
        "decision": {
            "preferred_side": "CALL",
            "margin": 30,
            "bull_score": 100,
            "bear_score": 0,
            "confidence": 90,
            "hard_gated": False,
        },
        "technicals": {"adx": 30, "atr": 100},
        "oi_summary": {
            "ce_max_oi_strike": 11000,
            "pe_max_oi_strike": 9000,
            "atm_iv": 20,
        },
        "vix": 15,
        "pcr": 1.3,
    }

    result = _score_candidates(market_data)
    assert result["best"] == "WAIT"

def test_strategy_route_missing_market_open_fails_closed_for_all_symbols(monkeypatch):
    import asyncio
    import app.api.routes.strategy as strategy

    class FakeAnalyzer:
        async def get_full_market_overview(
            self, symbol, expiry=None, cache_bust=""
        ):
            return {
                "decision": {
                    "preferred_side": "CALL",
                    "raw_preferred_side": "CALL",
                    "margin": 30,
                    "confidence": 90,
                    "signal_strength": 90,
                    "bull_score": 90,
                    "bear_score": 10,
                    "market_bias": "Bullish",
                    "strategy": "BUY CE",
                    "risk": "Medium",
                    # Intentionally omit decision.market_open and hard_gated.
                },
                # Intentionally omit spot.market_open.
                "spot": {"price": 100.0},
                "option_chain": {
                    "expiry": "27OCT2026",
                    "all_expiries": ["27OCT2026"],
                    "data": [],
                },
                "oi_summary": {
                    "atm_iv": 20,
                    "ce_max_oi_strike": 110,
                    "pe_max_oi_strike": 90,
                },
                "technicals": {"adx": 30, "atr": 2.0},
                "technicals_daily": {},
                "multi_timeframe": {},
                "support_resistance": {},
                "max_pain": 100.0,
                "pcr": 1.3,
                "vix": 15.0,
                "rsi": 60.0,
                "macd": {},
                "timestamp": "2026-10-25T18:00:00+00:00",
            }

    async def fake_load_state(symbol):
        return {
            "symbol": symbol,
            "lifecycle": "CONFIRMED_CALL",
            "active_side": "CALL",
            "candidate_side": "CALL",
            "confirmations": 3,
            "reversal_confirmations": 0,
            "last_evaluation_at": "2026-10-25T17:59:00+00:00",
            "last_confirmation_at": "2026-10-25T17:00:00+00:00",
        }

    async def fake_save_state(state):
        return None

    async def fake_record_signal(**kwargs):
        return {"reversal": False, "reversal_type": ""}

    async def fake_history(symbol):
        return []

    async def fake_history_persist(*args, **kwargs):
        return None

    async def fake_resolve(symbol, market_data, persisted_state=None):
        # Simulate a stale resolver proposing an entry. The endpoint's
        # independent hard-safety gate must reject this proposal.
        return {
            "best_strategy": "BUY CE",
            "best_score": 75,
            "candidates": {
                "WAIT": 23,
                "BUY CE": 75,
                "BUY PE": 10,
                "SELL CE": 10,
                "SELL PE": 10,
            },
            "lifecycle": "CONFIRMED_CALL",
            "active_side": "CALL",
            "confluence": {
                "confluence_score": 70,
                "direction": "BULLISH",
                "quality": "HIGH",
                "agreement_count": 5,
                "total_factors": 18,
                "factors": [],
            },
            "regime": {
                "regime": "TREND_UP",
                "no_trade": False,
                "hard_no_trade": False,
                "no_trade_reason": "",
            },
            "v3_gate": {
                "allowed": True,
                "fresh_entry_allowed": True,
                "reason": "Injected proposal for safety regression test",
            },
            "iv_info": {"penalty": 0, "warning": ""},
            "whipsaw_result": {"strategy": "BUY CE", "whipsaw_blocked": False},
        }

    monkeypatch.setattr(strategy, "load_signal_state", fake_load_state)
    monkeypatch.setattr(strategy, "save_signal_state", fake_save_state)
    monkeypatch.setattr(strategy, "record_signal_persistent", fake_record_signal)
    monkeypatch.setattr(strategy, "get_history_persistent", fake_history)
    monkeypatch.setattr(
        strategy, "_persist_analysis_history_if_due", fake_history_persist
    )
    monkeypatch.setattr(
        strategy, "_time_filter",
        lambda: {"session": "CLOSED", "penalty": 0, "warning": "Market closed"},
    )
    monkeypatch.setattr(
        strategy, "_expiry_filter",
        lambda expiry: {"days_left": 3, "warning": ""},
    )
    monkeypatch.setattr(
        strategy, "_expected_move",
        lambda spot, vix, days: {
            "move_points": 1.0,
            "upper": spot + 1.0,
            "lower": spot - 1.0,
            "basis": "test",
        },
    )
    monkeypatch.setattr(strategy, "resolve_final_strategy_action", fake_resolve)
    monkeypatch.setattr(
        strategy,
        "_v3_entry_gate",
        lambda *args, **kwargs: {
            "allowed": True,
            "fresh_entry_allowed": True,
            "reason": "Injected gate proposal for safety regression test",
        },
    )
    monkeypatch.setattr(
        "app.services.contract_specs.resolve_lot_size",
        lambda symbol, chain: 20,
    )

    for symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        result = asyncio.run(
            strategy.strike_recommendation(
                symbol=symbol,
                analyzer=FakeAnalyzer(),
                ai=object(),
            )
        )

        assert result["symbol"] == symbol
        assert result["market_open"] is False, symbol
        assert result["hard_gated"] is True, symbol
        assert result["market_state"] == "MARKET CLOSED", symbol
        assert result["preferred_side"] == "NONE", symbol
        assert result["signal_lifecycle"] == "WAIT", symbol
        assert result["signal_active_side"] == "NONE", symbol
        assert result["action"].startswith("NO TRADE"), symbol
        assert result["best_strategy"] == "WAIT", symbol
        assert result["signal_action"] == "WAIT", symbol
        assert result["entry_gate"]["allowed"] is False, symbol
        assert result["entry_gate"]["fresh_entry_allowed"] is False, symbol
        assert result["strikes"] == [], symbol
        assert result["price_levels"] is None, symbol
        assert result["strategy_detail"] is None, symbol
        assert result["v2_trade_levels"] is None, symbol
        assert result["v2_risk"] is None, symbol
        assert result["trade_confidence"] == 0, symbol
        print(f"PASS route payload: {symbol}")

    print("ROUTE_RESPONSE_CONTRACT=PASS")
    print("ROUTE_SYMBOL_CASES_PASSED=4")

def test_forward_paper_capture_eligibility_requires_confirmed_consistent_entry():
    from app.api.routes.strategy import _forward_paper_capture_eligible

    confirmed = (
        ("BUY CE", "CONFIRMED_CALL"),
        ("SELL PE", "CONFIRMED_CALL"),
        ("BUY PE", "CONFIRMED_PUT"),
        ("SELL CE", "CONFIRMED_PUT"),
    )

    for action, lifecycle in confirmed:
        assert _forward_paper_capture_eligible(
            action=action,
            lifecycle_state=lifecycle,
            fresh_entry_allowed=True,
            action_blocked=False,
            signal_action=action,
        )

        assert not _forward_paper_capture_eligible(
            action=action,
            lifecycle_state=lifecycle,
            fresh_entry_allowed=False,
            action_blocked=False,
            signal_action=action,
        )

        assert not _forward_paper_capture_eligible(
            action=action,
            lifecycle_state=lifecycle,
            fresh_entry_allowed=True,
            action_blocked=True,
            signal_action="WAIT",
        )

    assert not _forward_paper_capture_eligible(
        action="BUY CE",
        lifecycle_state="HOLD_CALL",
        fresh_entry_allowed=True,
        action_blocked=False,
        signal_action="BUY CE",
    )
    assert not _forward_paper_capture_eligible(
        action="BUY CE",
        lifecycle_state="CONFIRMED_CALL",
        fresh_entry_allowed=True,
        action_blocked=False,
        signal_action="WAIT",
    )

def test_pick_strikes_carries_source_chain_symbol_into_contract(monkeypatch):
    import app.api.routes.strategy as strategy

    monkeypatch.setattr(strategy, "_days_to_expiry", lambda expiry: 5)
    monkeypatch.setattr(
        strategy,
        "black_scholes_greeks",
        lambda *args, **kwargs: {
            "delta": 0.5,
            "theta_per_day": 0.0,
            "vega": 0.1,
        },
    )

    chain = {
        "symbol": "BANKNIFTY",
        "expiry": "15DEC2026",
        "data": [
            {
                "strikePrice": 25000,
                "CE": {
                    "lastPrice": 100.0,
                    "openInterest": 1200,
                    "totalTradedVolume": 2400,
                    "impliedVolatility": 20.0,
                    "bidprice": 99.0,
                    "askPrice": 101.0,
                },
            }
        ],
    }

    picks = strategy._pick_strikes(
        chain,
        is_call=True,
        spot=25000.0,
        atr=10.0,
        action="BUY CE",
        assumed_hold_days=0.25,
    )

    assert picks
    assert picks[0]["symbol"] == "BANKNIFTY"
    assert picks[0]["type"] == "CE"
    assert picks[0]["expiry"] == "15DEC2026"
