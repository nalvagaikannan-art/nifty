from app.services.confluence_engine import _eval_oi_change
from app.services.decision_engine import (
    _oi_change_for_scoring,
    _score_oi_change,
)


def test_unavailable_tracked_oi_does_not_fall_back_to_provider_values():
    market_data = {
        "oi_change": {"ce_change": 10000, "pe_change": 0},
        "oi_change_tracked": {
            "available": False,
            "status": "unavailable",
            "reason": "Reference OI coverage below 70%",
        },
    }

    score_input = _oi_change_for_scoring(market_data)
    assert score_input["available"] is False
    direction, score, reason = _score_oi_change(score_input)
    assert direction == "neutral"
    assert score == 0
    assert "OI change unavailable" in reason

    factor = _eval_oi_change(market_data)
    assert factor["direction"] == "neutral"
    assert factor["score"] == 0
    assert "unavailable" in factor["reason"].lower()


def test_complete_tracked_oi_takes_precedence_over_provider_day_change():
    market_data = {
        "oi_change": {"ce_change": 100000, "pe_change": 0},
        "oi_change_tracked": {
            "available": True,
            "status": "available",
            "ce_oi_change": 10,
            "pe_oi_change": 30,
            "coverage": {
                "ce_coverage_pct": 100.0,
                "pe_coverage_pct": 100.0,
            },
        },
    }

    score_input = _oi_change_for_scoring(market_data)
    assert score_input["available"] is True
    assert score_input["ce_change"] == 10
    assert score_input["pe_change"] == 30
    assert _score_oi_change(score_input)[0] == "bull"

    factor = _eval_oi_change(market_data)
    assert factor["direction"] == "bullish"
    assert factor["score"] > 0


def test_available_tracked_oi_with_missing_side_is_unavailable():
    market_data = {
        "oi_change": {"ce_change": 5000, "pe_change": 0},
        "oi_change_tracked": {
            "available": True,
            "ce_oi_change": 50,
            "pe_oi_change": None,
        },
    }

    assert _oi_change_for_scoring(market_data)["available"] is False
    factor = _eval_oi_change(market_data)
    assert factor["direction"] == "neutral"
    assert factor["score"] == 0
    assert "incomplete" in factor["reason"].lower()


def test_legacy_provider_only_oi_input_remains_supported():
    market_data = {"oi_change": {"ce_change": 100, "pe_change": 0}}

    score_input = _oi_change_for_scoring(market_data)
    assert score_input["available"] is True
    assert score_input["ce_change"] == 100
    assert score_input["pe_change"] == 0
    assert _score_oi_change(score_input)[0] == "bear"

    factor = _eval_oi_change(market_data)
    assert factor["direction"] == "bearish"
    assert factor["score"] > 0
