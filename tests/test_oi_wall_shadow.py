import pandas as pd

from app.services.history_service import _build_oi_wall_evidence


def _chain():
    return pd.DataFrame([
        {"strike": 24000.0, "ce_oi": 1000, "ce_ltp": 75.0,
         "pe_oi": 2200, "pe_ltp": 12.0},
        {"strike": 24500.0, "ce_oi": 5000, "ce_ltp": 100.0,
         "pe_oi": 400, "pe_ltp": 50.0},
        {"strike": 25000.0, "ce_oi": 4000, "ce_ltp": 70.0,
         "pe_oi": 300, "pe_ltp": 80.0},
    ])


def test_reports_oi_and_premium_change_at_current_max_oi_walls():
    current = _chain()
    previous = {
        (24500.0, "CE"): (4500, 110.0),
        (24000.0, "PE"): (2000, 15.0),
    }

    result = _build_oi_wall_evidence(current, previous)

    assert result["available"] is True
    assert result["ce"]["available"] is True
    assert result["ce"]["strike"] == 24500.0
    assert result["ce"]["oi_change"] == 500
    assert result["ce"]["price_change"] == -10.0
    assert result["ce"]["label"] == "Short Buildup"

    assert result["pe"]["available"] is True
    assert result["pe"]["strike"] == 24000.0
    assert result["pe"]["oi_change"] == 200
    assert result["pe"]["price_change"] == -3.0
    assert result["pe"]["label"] == "Short Buildup"


def test_wall_evidence_rejects_nonpositive_ltp():
    previous = {
        (24500.0, "CE"): (4500, 110.0),
        (24000.0, "PE"): (2000, 15.0),
    }

    current = _chain()
    current.loc[current["ce_oi"].idxmax(), "ce_ltp"] = 0.0
    result = _build_oi_wall_evidence(current, previous)
    assert result["ce"]["available"] is False
    assert result["ce"]["reason"] == "invalid_current_wall_price"

    current = _chain()
    invalid_previous = dict(previous)
    invalid_previous[(24500.0, "CE")] = (4500, 0.0)
    result = _build_oi_wall_evidence(current, invalid_previous)
    assert result["ce"]["available"] is False
    assert result["ce"]["reason"] == "invalid_baseline_price"


def test_missing_baseline_is_explicit_per_wall():
    current = _chain()
    previous = {(24500.0, "CE"): (4500, 110.0)}

    result = _build_oi_wall_evidence(current, previous)

    assert result["available"] is True
    assert result["ce"]["available"] is True
    assert result["pe"]["available"] is False
    assert result["pe"]["reason"] == "wall_strike_missing_from_baseline"


def test_no_comparable_wall_baseline_is_not_fabricated():
    result = _build_oi_wall_evidence(_chain(), {})

    assert result["available"] is False
    assert result["ce"]["available"] is False
    assert result["pe"]["available"] is False
