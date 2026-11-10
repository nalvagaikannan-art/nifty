from copy import deepcopy
from datetime import datetime
from uuid import UUID

import pytest

from app.services.entry_risk_authority import (
    build_entry_risk_authority,
    is_entry_risk_approved,
)


def approved_authority(**overrides):
    inputs = {
        "action": "BUY CE",
        "risk_assessment": {
            "allowed": True,
            "lots": 2,
            "quantity": 130,
            "max_loss": 2600,
            "max_risk_amount": 3000,
            "risk_per_lot": 1300,
            "no_trade_reasons": [],
            "reasons": ["Risk checks passed"],
            "warnings": [],
        },
        "selected_contract": {
            "symbol": "NIFTY",
            "expiry": "2026-11-12",
            "strike": 26000,
            "type": "CE",
            "entry_price": 120,
            "sl": 100,
        },
        "trade_levels": {
            "option_entry": 120,
            "option_sl": 100,
            "option_rr_ratio": 2.0,
        },
        "lot_size": 65,
        "risk_budget": 3000,
    }
    inputs.update(overrides)
    if "selected_contract" not in overrides:
        inputs["selected_contract"]["type"] = inputs["action"].split()[1]
    return build_entry_risk_authority(**inputs)


def test_valid_buy_ce_assessment_is_approved():
    authority = approved_authority()

    assert authority["schema_version"] == 1
    assert UUID(authority["assessment_id"])
    assessed_at = datetime.fromisoformat(authority["assessed_at"])
    assert assessed_at.utcoffset().total_seconds() == 0
    assert authority["status"] == "APPROVED"
    assert authority["allowed"] is True
    assert is_entry_risk_approved(authority) is True


def test_valid_buy_pe_assessment_is_approved():
    authority = approved_authority(action="BUY PE")
    assert authority["status"] == "APPROVED"
    assert is_entry_risk_approved(authority) is True


def test_valid_rejection_preserves_all_reasons():
    assessment = {
        "allowed": False,
        "lots": 0,
        "quantity": 0,
        "max_loss": 0,
        "no_trade_reasons": ["Risk budget exceeded", "Liquidity insufficient"],
        "warnings": ["Low liquidity"],
    }
    authority = approved_authority(risk_assessment=assessment)

    assert authority["status"] == "REJECTED"
    assert authority["allowed"] is False
    assert authority["reasons"] == assessment["no_trade_reasons"]
    assert is_entry_risk_approved(authority) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("lots", 1),
        ("quantity", 65),
        ("max_loss", 1),
    ],
)
def test_rejection_with_nonzero_sizing_is_error(field, value):
    assessment = {
        "allowed": False,
        "lots": 0,
        "quantity": 0,
        "max_loss": 0,
        "no_trade_reasons": ["Rejected"],
    }
    assessment[field] = value
    authority = approved_authority(risk_assessment=assessment)

    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_rejection_without_reason_is_error():
    assessment = {
        "allowed": False,
        "lots": 0,
        "quantity": 0,
        "max_loss": 0,
        "no_trade_reasons": [],
    }
    authority = approved_authority(risk_assessment=assessment)
    assert authority["status"] == "ERROR"
    assert is_entry_risk_approved(authority) is False


def test_missing_assessment_is_unassessed():
    authority = approved_authority(risk_assessment=None)
    assert authority["status"] == "UNASSESSED"
    assert authority["allowed"] is False


def test_assessment_exception_is_error():
    authority = approved_authority(assessment_error=RuntimeError("risk engine failed"))
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_sell_action_is_not_supported_by_current_risk_model():
    authority = approved_authority(action="SELL CE")
    assert authority["status"] == "UNASSESSED"
    assert authority["allowed"] is False


@pytest.mark.parametrize("verdict", ["true", 1, None])
def test_non_boolean_verdict_is_error(verdict):
    assessment = {
        "allowed": verdict,
        "lots": 2,
        "quantity": 130,
        "max_loss": 2600,
        "risk_per_lot": 1300,
    }
    authority = approved_authority(risk_assessment=assessment)
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_boolean_lot_count_is_not_accepted():
    assessment = {
        "allowed": True,
        "lots": True,
        "quantity": 65,
        "max_loss": 1300,
        "risk_per_lot": 1300,
    }
    authority = approved_authority(risk_assessment=assessment)
    assert authority["status"] == "ERROR"


def test_approval_without_contract_is_error():
    authority = approved_authority(selected_contract={})
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_approval_without_trade_levels_is_error():
    authority = approved_authority(trade_levels=None)
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("schema_version",), 2),
        (("assessment_id",), "not-a-uuid"),
        (("assessed_at",), "2026-11-06T12:00:00"),
        (("action",), "SELL CE"),
        (("allowed",), False),
        (("reasons",), "not-a-list"),
        (("snapshot", "selected_contract"), {}),
        (("snapshot", "entry_price"), 90),
        (("snapshot", "stop_loss_price"), 120),
        (("snapshot", "lot_size"), 0),
        (("snapshot", "lots"), 0),
        (("snapshot", "quantity"), 129),
        (("snapshot", "max_loss"), 3001),
        (("snapshot", "risk_budget"), 0),
        (("snapshot", "risk_per_lot"), 999),
        (("snapshot", "option_rr_ratio"), 0),
        (("snapshot", "warnings"), None),
    ],
)
def test_validator_rejects_tampered_approval_records(path, value):
    authority = approved_authority()
    target = authority
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert is_entry_risk_approved(authority) is False


@pytest.mark.parametrize("authority", [None, {}, [], "APPROVED", 1])
def test_validator_rejects_non_mapping_records(authority):
    assert is_entry_risk_approved(authority) is False


def test_actual_route_trade_level_schema_is_accepted():
    authority = approved_authority(
        trade_levels={
            "option_entry": 120,
            "option_sl": 100,
            "option_rr_ratio": 2.0,
        }
    )
    assert authority["status"] == "APPROVED"
    assert is_entry_risk_approved(authority) is True


def test_engine_max_risk_amount_is_required():
    assessment = {
        "allowed": True,
        "lots": 2,
        "quantity": 130,
        "max_loss": 2600,
        "risk_per_lot": 1300,
        "no_trade_reasons": [],
        "reasons": [],
        "warnings": [],
    }
    authority = approved_authority(risk_assessment=assessment)
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_conflicting_caller_budget_is_error():
    authority = approved_authority(risk_budget=2000)
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_contract_entry_must_match_canonical_trade_levels():
    contract = {
        "symbol": "NIFTY", "strike": 26000, "type": "CE",
        "expiry": "2026-11-12", "entry_price": 121, "sl": 100,
    }
    authority = approved_authority(selected_contract=contract)
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_contract_side_must_match_action():
    contract = {
        "symbol": "NIFTY", "strike": 26000, "type": "CE",
        "expiry": "2026-11-12", "entry_price": 120, "sl": 100,
    }
    authority = approved_authority(action="BUY PE", selected_contract=contract)
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_unsupported_contract_symbol_is_error():
    contract = {
        "symbol": "UNKNOWN", "strike": 26000, "type": "CE",
        "expiry": "2026-11-12", "entry_price": 120, "sl": 100,
    }
    authority = approved_authority(selected_contract=contract)
    assert authority["status"] == "ERROR"
    assert authority["allowed"] is False


def test_validator_rejects_contract_price_tampering():
    authority = approved_authority()
    authority["snapshot"]["selected_contract"]["entry_price"] = 121
    assert is_entry_risk_approved(authority) is False


def test_validator_rejects_contract_identity_tampering():
    authority = approved_authority()
    authority["snapshot"]["selected_contract"]["symbol"] = "UNKNOWN"
    assert is_entry_risk_approved(authority) is False


def test_validator_rejects_inconsistent_max_loss():
    authority = approved_authority()
    authority["snapshot"]["max_loss"] = 2500
    assert is_entry_risk_approved(authority) is False
