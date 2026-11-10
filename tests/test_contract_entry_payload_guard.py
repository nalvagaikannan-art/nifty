from app.api.routes.analysis import _finalize_ai_analysis_result


def _payload(
    *, lifecycle, action, gate, detail=None, regime=None,
    recommended_option=None, price_levels=None, recommended_strike="ATM",
):
    return {
        "signal_lifecycle": lifecycle,
        "signal_action": action,
        "v3_gate": gate,
        "hard_gated": False,
        "market_regime_no_trade": False,
        "market_regime": regime or {"regime": "TREND_DOWN", "no_trade": False},
        "recommended_option": recommended_option or {"available": True, "strike": 54100},
        "recommended_strike": recommended_strike,
        "strategy_detail": detail or {
            "name": "Directional Put Bias",
            "legs": [{"action": "BUY", "type": "PE", "strike": 54100}],
        },
        "price_levels": price_levels or {"entry": 300, "stop": 200, "target": 450},
    }


def test_hold_bias_clears_directional_contract_payload():
    result = _payload(
        lifecycle="HOLD_PUT",
        action="HOLD PUT BIAS",
        gate={"allowed": True, "fresh_entry_allowed": False},
    )

    result = _finalize_ai_analysis_result(result)

    assert result["recommended_option"] is None
    assert result["recommended_strike"] == "NONE"
    assert result["strategy_detail"] is None
    assert result["price_levels"] is None


def test_wait_clears_directional_contract_payload():
    result = _payload(
        lifecycle="WAIT",
        action="WAIT",
        gate={"allowed": False, "fresh_entry_allowed": False},
    )

    result = _finalize_ai_analysis_result(result)

    assert result["recommended_option"] is None
    assert result["recommended_strike"] == "NONE"
    assert result["strategy_detail"] is None
    assert result["price_levels"] is None


def test_watch_does_not_expose_directional_entry_details():
    result = _payload(
        lifecycle="WATCH_PUT",
        action="WAIT",
        gate={"allowed": False, "fresh_entry_allowed": False},
    )

    result = _finalize_ai_analysis_result(result)

    assert result["recommended_option"] is None
    assert result["recommended_strike"] == "NONE"
    assert result["strategy_detail"] is None
    assert result["price_levels"] is None


def test_confirmed_fresh_entry_retains_contract_payload():
    expected_option = {"available": True, "type": "PE", "strike": 54100}
    expected_detail = {"name": "Directional Put Bias", "legs": [{"strike": 54100}]}
    expected_levels = {"entry": 300, "stop": 200, "target": 450}
    result = _payload(
        lifecycle="CONFIRMED_PUT",
        action="BUY PE",
        gate={"allowed": True, "fresh_entry_allowed": True},
        detail=expected_detail,
        regime={"regime": "TREND_DOWN", "no_trade": False},
        recommended_option=expected_option,
        price_levels=expected_levels,
        recommended_strike="ATM",
    )

    from app.services.entry_risk_authority import build_entry_risk_authority

    lot_size = 65
    entry = 120.0
    stop = 100.0
    risk_per_lot = (entry - stop) * lot_size

    result["fresh_entry_allowed"] = True
    result["entry_risk_authority"] = build_entry_risk_authority(
        action="BUY PE",
        risk_assessment={
            "allowed": True,
            "lots": 2,
            "quantity": 130,
            "max_loss": risk_per_lot * 2,
            "max_risk_amount": 3000,
            "risk_per_lot": risk_per_lot,
            "no_trade_reasons": [],
            "reasons": ["Controlled test approval"],
            "warnings": [],
        },
        selected_contract={
            "symbol": "NIFTY",
            "expiry": "2026-11-12",
            "strike": 54100,
            "type": "PE",
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

    result = _finalize_ai_analysis_result(result)

    assert result["recommended_option"] == expected_option
    assert result["recommended_strike"] == "ATM"
    assert result["strategy_detail"] == expected_detail
    assert result["price_levels"] == expected_levels


def test_wait_preserves_explicit_range_iron_condor_context_only():
    detail = {
        "name": "Iron Condor",
        "legs": [
            {"action": "SELL", "type": "CE", "strike": 55000},
            {"action": "BUY", "type": "CE", "strike": 55500},
            {"action": "SELL", "type": "PE", "strike": 54000},
            {"action": "BUY", "type": "PE", "strike": 53500},
        ],
    }
    result = _payload(
        lifecycle="WAIT",
        action="WAIT",
        gate={"allowed": False, "fresh_entry_allowed": False},
        detail=detail,
        regime={"regime": "RANGE", "no_trade": False, "hard_no_trade": False},
    )

    result = _finalize_ai_analysis_result(result)

    assert result["recommended_option"] is None
    assert result["recommended_strike"] == "NONE"
    assert result["strategy_detail"] == detail
    assert result["price_levels"] is None
