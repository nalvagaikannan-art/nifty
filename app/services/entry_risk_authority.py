"""Fail-closed authority for entry-risk approval.

This module normalizes an existing risk-engine verdict. It does not
calculate risk or place orders.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping
from uuid import UUID, uuid4


SUPPORTED_ACTIONS = {"BUY CE", "BUY PE"}
SUPPORTED_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _reasons(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def build_entry_risk_authority(
    *,
    action: str,
    risk_assessment: Mapping[str, Any] | None = None,
    assessment_error: Any = None,
    selected_contract: Mapping[str, Any] | None = None,
    trade_levels: Mapping[str, Any] | None = None,
    lot_size: Any = None,
    risk_budget: Any = None,
) -> dict[str, Any]:
    """Build a versioned, fail-closed entry-risk authority record."""
    normalized_action = str(action or "").strip().upper()
    authority: dict[str, Any] = {
        "schema_version": 1,
        "assessment_id": str(uuid4()),
        "assessed_at": datetime.now(timezone.utc).isoformat(),
        "status": "UNASSESSED",
        "allowed": False,
        "action": normalized_action,
        "reasons": [],
        "snapshot": {},
    }

    def finish(status: str, reasons: list[str]) -> dict[str, Any]:
        authority["status"] = status
        authority["allowed"] = status == "APPROVED"
        authority["reasons"] = reasons
        return authority

    if assessment_error is not None:
        return finish("ERROR", [f"Risk assessment error: {assessment_error}"])

    if normalized_action not in SUPPORTED_ACTIONS:
        return finish("UNASSESSED", ["Action is not supported by the current risk model"])

    if not isinstance(risk_assessment, Mapping):
        return finish("UNASSESSED", ["Risk assessment is missing or invalid"])

    verdict = risk_assessment.get("allowed")
    if type(verdict) is not bool:
        return finish("ERROR", ["Risk assessment allowed flag must be a boolean"])

    assessment_reasons = _reasons(risk_assessment.get("no_trade_reasons"))
    warnings = _reasons(risk_assessment.get("warnings"))

    if verdict is False:
        lots = _integer(risk_assessment.get("lots"))
        quantity = _integer(risk_assessment.get("quantity"))
        max_loss = _number(risk_assessment.get("max_loss"))
        if lots != 0 or quantity != 0 or max_loss != 0:
            return finish("ERROR", ["Rejected assessment contains non-zero position sizing"])
        if not assessment_reasons:
            return finish("ERROR", ["Rejected assessment has no rejection reason"])
        authority["snapshot"] = {"warnings": warnings}
        return finish("REJECTED", assessment_reasons)

    if not isinstance(selected_contract, Mapping) or not selected_contract:
        return finish("ERROR", ["Selected option contract is missing"])
    if not isinstance(trade_levels, Mapping):
        return finish("ERROR", ["Trade levels are missing"])

    raw_no_trade_reasons = risk_assessment.get("no_trade_reasons")
    if not isinstance(raw_no_trade_reasons, (list, tuple)) or raw_no_trade_reasons:
        return finish("ERROR", ["Approved assessment contains invalid no-trade reasons"])

    # Match the actual _pick_strikes() contract schema.
    symbol = str(selected_contract.get("symbol") or "").strip().upper()
    strike = _number(selected_contract.get("strike"))
    contract_type = str(selected_contract.get("type") or "").strip().upper()
    expiry = str(selected_contract.get("expiry") or "").strip()
    contract_entry = _number(selected_contract.get("entry_price"))
    contract_stop = _number(selected_contract.get("sl"))

    if symbol not in SUPPORTED_SYMBOLS:
        return finish("ERROR", ["Selected contract symbol is missing or unsupported"])
    if strike is None or strike <= 0:
        return finish("ERROR", ["Selected contract strike is invalid"])
    if contract_type not in ("CE", "PE") or contract_type != normalized_action.split()[1]:
        return finish("ERROR", ["Selected contract option type does not match the action"])
    if not expiry:
        return finish("ERROR", ["Selected contract expiry is missing"])

    # Match the actual calculate_trade_levels() output schema.
    entry = _number(trade_levels.get("option_entry"))
    stop = _number(trade_levels.get("option_sl"))
    option_rr = _number(trade_levels.get("option_rr_ratio"))

    if entry is None or stop is None or entry <= stop:
        return finish("ERROR", ["Canonical option entry and stop-loss prices are invalid"])
    if contract_entry is None or contract_stop is None:
        return finish("ERROR", ["Selected contract premium levels are missing"])
    if not math.isclose(contract_entry, entry, rel_tol=0.0, abs_tol=0.05):
        return finish("ERROR", ["Selected contract entry does not match canonical trade levels"])
    if not math.isclose(contract_stop, stop, rel_tol=0.0, abs_tol=0.05):
        return finish("ERROR", ["Selected contract stop does not match canonical trade levels"])

    size = _integer(lot_size)
    # The engine's own max_risk_amount is the authoritative per-trade budget.
    budget = _number(risk_assessment.get("max_risk_amount"))
    supplied_budget = _number(risk_budget) if risk_budget is not None else None
    lots = _integer(risk_assessment.get("lots"))
    quantity = _integer(risk_assessment.get("quantity"))
    max_loss = _number(risk_assessment.get("max_loss"))
    risk_per_lot = _number(risk_assessment.get("risk_per_lot"))

    if size is None or size <= 0:
        return finish("ERROR", ["Lot size is invalid"])
    if budget is None or budget <= 0:
        return finish("ERROR", ["Risk engine per-trade budget is missing or invalid"])
    if risk_budget is not None and (
        supplied_budget is None
        or not math.isclose(supplied_budget, budget, rel_tol=0.0, abs_tol=1.0)
    ):
        return finish("ERROR", ["Supplied risk budget conflicts with risk-engine budget"])
    if lots is None or not 1 <= lots <= 5:
        return finish("ERROR", ["Approved lot count is outside the permitted range"])
    if quantity is None or quantity != lots * size:
        return finish("ERROR", ["Quantity does not match lots multiplied by lot size"])
    if max_loss is None or max_loss < 0 or max_loss > budget:
        return finish("ERROR", ["Maximum loss exceeds the risk budget or is invalid"])
    if risk_per_lot is None or risk_per_lot <= 0:
        return finish("ERROR", ["Risk per lot is missing or invalid"])
    expected_risk = (entry - stop) * size
    if not math.isclose(risk_per_lot, expected_risk, rel_tol=0.02, abs_tol=1.0):
        return finish("ERROR", ["Risk per lot does not match entry, stop-loss and lot size"])
    if max_loss is None or not math.isclose(
        max_loss, risk_per_lot * lots, rel_tol=0.0, abs_tol=1.0
    ):
        return finish("ERROR", ["Maximum loss does not match risk per lot multiplied by lots"])
    if option_rr is None or option_rr <= 0:
        return finish("ERROR", ["Option risk-reward ratio is invalid"])

    contract_snapshot = {
        "symbol": symbol,
        "strike": strike,
        "type": contract_type,
        "expiry": expiry,
        "entry_price": contract_entry,
        "sl": contract_stop,
    }
    for key in (
        "ltp", "bid", "ask", "price_source", "live_ws",
        "live_ws_source", "live_ws_received_at", "oi", "volume",
    ):
        if key in selected_contract:
            contract_snapshot[key] = selected_contract[key]

    authority["snapshot"] = {
        "selected_contract": contract_snapshot,
        "entry_price": entry,
        "stop_loss_price": stop,
        "lot_size": size,
        "risk_budget": budget,
        "lots": lots,
        "quantity": quantity,
        "max_loss": max_loss,
        "risk_per_lot": risk_per_lot,
        "option_rr_ratio": option_rr,
        "warnings": warnings,
    }
    return finish("APPROVED", _reasons(risk_assessment.get("reasons")))


def is_entry_risk_approved(authority: Any) -> bool:
    """Return True only for a complete, internally consistent approval record."""
    try:
        if not isinstance(authority, Mapping):
            return False
        if type(authority.get("schema_version")) is not int:
            return False
        if authority.get("schema_version") != 1:
            return False
        if authority.get("status") != "APPROVED":
            return False
        if authority.get("allowed") is not True:
            return False
        if authority.get("action") not in SUPPORTED_ACTIONS:
            return False

        assessment_id = authority.get("assessment_id")
        if not isinstance(assessment_id, str):
            return False
        parsed_id = UUID(assessment_id)
        if str(parsed_id) != assessment_id:
            return False

        assessed_at = authority.get("assessed_at")
        if not isinstance(assessed_at, str):
            return False
        parsed_time = datetime.fromisoformat(assessed_at.replace("Z", "+00:00"))
        if parsed_time.tzinfo is None or parsed_time.utcoffset().total_seconds() != 0:
            return False

        reasons = authority.get("reasons")
        if not isinstance(reasons, list) or not all(isinstance(x, str) for x in reasons):
            return False

        snapshot = authority.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return False
        contract = snapshot.get("selected_contract")
        if not isinstance(contract, Mapping) or not contract:
            return False
        symbol = str(contract.get("symbol") or "").strip().upper()
        strike = _number(contract.get("strike"))
        contract_type = str(contract.get("type") or "").strip().upper()
        expiry = str(contract.get("expiry") or "").strip()
        contract_entry = _number(contract.get("entry_price"))
        contract_stop = _number(contract.get("sl"))
        if symbol not in SUPPORTED_SYMBOLS or strike is None or strike <= 0:
            return False
        if contract_type != authority["action"].split()[1] or not expiry:
            return False
        if contract_entry is None or contract_stop is None:
            return False
        warnings = snapshot.get("warnings")
        if not isinstance(warnings, list) or not all(isinstance(x, str) for x in warnings):
            return False

        entry = _number(snapshot.get("entry_price"))
        stop = _number(snapshot.get("stop_loss_price"))
        size = _integer(snapshot.get("lot_size"))
        budget = _number(snapshot.get("risk_budget"))
        lots = _integer(snapshot.get("lots"))
        quantity = _integer(snapshot.get("quantity"))
        max_loss = _number(snapshot.get("max_loss"))
        risk_per_lot = _number(snapshot.get("risk_per_lot"))
        option_rr = _number(snapshot.get("option_rr_ratio"))

        if entry is None or stop is None or entry <= stop:
            return False
        if size is None or size <= 0:
            return False
        if budget is None or budget <= 0:
            return False
        if lots is None or not 1 <= lots <= 5:
            return False
        if quantity is None or quantity != lots * size:
            return False
        if max_loss is None or not 0 <= max_loss <= budget:
            return False
        if risk_per_lot is None or risk_per_lot <= 0:
            return False
        expected_risk = (entry - stop) * size
        if not math.isclose(risk_per_lot, expected_risk, rel_tol=0.02, abs_tol=1.0):
            return False
        if not math.isclose(contract_entry, entry, rel_tol=0.0, abs_tol=0.05):
            return False
        if not math.isclose(contract_stop, stop, rel_tol=0.0, abs_tol=0.05):
            return False
        if not math.isclose(max_loss, risk_per_lot * lots, rel_tol=0.0, abs_tol=1.0):
            return False
        if option_rr is None or option_rr <= 0:
            return False
        return True
    except (TypeError, ValueError, OverflowError, AttributeError):
        return False
