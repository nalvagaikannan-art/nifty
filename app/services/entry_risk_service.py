"""Shared fail-closed BUY-option risk assessment.

This service normalizes inputs, invokes the existing risk engine, and
creates the versioned authority record. It never places orders.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Callable

from app.config import settings
from app.services.entry_risk_authority import (
    SUPPORTED_SYMBOLS,
    build_entry_risk_authority,
    is_entry_risk_approved,
)
from app.services.paper_trading import get_daily_pnl, get_open_trades
from app.services.risk_engine import assess_risk
from app.utils.helpers import days_to_expiry


SUPPORTED_BUY_ACTIONS = {"BUY CE", "BUY PE"}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


async def assess_entry_risk_authority(
    *,
    action: str,
    symbol: str,
    market_data: Mapping[str, Any],
    confluence: Mapping[str, Any],
    regime: Mapping[str, Any],
    signal_strength: Any,
    selected_contract: Mapping[str, Any] | None,
    trade_levels: Mapping[str, Any] | None,
    lot_size: Any,
    risk_assessor: Callable[..., Any] | None = None,
    daily_pnl_loader: Callable[..., Any] | None = None,
    open_trades_loader: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Return the existing risk-engine verdict plus one validated authority.

    SELL and unsupported actions deliberately remain UNASSESSED. The
    current risk engine is only validated for long-option BUY CE/BUY PE.
    """
    normalized_action = str(action or "").strip().upper()
    contract = (
        dict(selected_contract)
        if isinstance(selected_contract, Mapping)
        else None
    )

    if normalized_action not in SUPPORTED_BUY_ACTIONS:
        authority = build_entry_risk_authority(
            action=normalized_action,
            selected_contract=contract,
            trade_levels=trade_levels,
            lot_size=lot_size,
        )
        return {
            "risk_assessment": None,
            "entry_risk_authority": authority,
            "risk_approved": False,
            "assessment_error": None,
        }

    risk_result = None
    assessment_error = None

    try:
        if not isinstance(market_data, Mapping):
            raise ValueError("Market data is missing")
        if not isinstance(contract, dict) or not contract:
            raise ValueError("Selected option contract is missing")
        if not isinstance(trade_levels, Mapping):
            raise ValueError("Canonical trade levels are missing")
        if not isinstance(confluence, Mapping):
            raise ValueError("Confluence evidence is missing")
        if not isinstance(regime, Mapping):
            raise ValueError("Market regime evidence is missing")

        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise ValueError("Symbol is missing")
        if normalized_symbol not in SUPPORTED_SYMBOLS:
            raise ValueError("Requested symbol is unsupported")

        market_symbol = str(
            market_data.get("symbol") or ""
        ).strip().upper()
        if market_symbol != normalized_symbol:
            raise ValueError(
                "Market data symbol does not match requested symbol"
            )

        chain = market_data.get("option_chain")
        if not isinstance(chain, Mapping):
            raise ValueError("Option chain snapshot is missing")

        chain_symbol = str(
            chain.get("symbol") or ""
        ).strip().upper()
        if chain_symbol != normalized_symbol:
            raise ValueError(
                "Option-chain symbol does not match requested symbol"
            )

        contract_symbol = str(
            contract.get("symbol") or ""
        ).strip().upper()
        if contract_symbol != normalized_symbol:
            raise ValueError(
                "Selected contract symbol does not match requested symbol"
            )

        chain_expiry = str(chain.get("expiry") or "").strip()
        expiry = str(contract.get("expiry") or "").strip()
        if not chain_expiry:
            raise ValueError("Option-chain expiry is missing")
        if not expiry:
            raise ValueError("Selected contract expiry is missing")
        if expiry.upper() != chain_expiry.upper():
            raise ValueError(
                "Selected contract expiry does not match option chain"
            )

        contract_type = str(contract.get("type") or "").strip().upper()
        expected_type = normalized_action.split()[1]
        if contract_type != expected_type:
            raise ValueError("Selected option type does not match action")

        entry = _finite_number(trade_levels.get("option_entry"))
        stop = _finite_number(trade_levels.get("option_sl"))
        rr = _finite_number(trade_levels.get("rr_ratio"))
        option_rr = _finite_number(trade_levels.get("option_rr_ratio"))

        if entry is None or stop is None or entry <= stop:
            raise ValueError("Canonical BUY option entry/stop levels are invalid")
        if rr is None or rr <= 0:
            raise ValueError("Structural risk-reward ratio is unavailable")
        if option_rr is None or option_rr <= 0:
            raise ValueError("Option premium risk-reward ratio is unavailable")

        # The selected contract must carry actual OI/volume observations.
        # Do not replace missing liquidity evidence with zero defaults.
        oi = _finite_number(contract.get("oi"))
        volume = _finite_number(contract.get("volume"))
        if (
            oi is None or volume is None
            or oi <= 0 or volume <= 0
        ):
            raise ValueError(
                "Selected contract OI/volume evidence is missing, zero or invalid"
            )

        dte = days_to_expiry(expiry)
        if type(dte) is not int:
            raise ValueError("Contract expiry could not be parsed")

        size = _finite_number(lot_size)
        if size is None or size <= 0 or not size.is_integer():
            raise ValueError("Resolved contract lot size is invalid")

        contract["entry_price"] = entry
        contract["sl"] = stop

        daily_loader = daily_pnl_loader or get_daily_pnl
        positions_loader = open_trades_loader or get_open_trades
        assessor = risk_assessor or assess_risk

        daily_pnl = await daily_loader()

        # MAX_OPEN_POSITIONS is a global limit. Passing None deliberately
        # requests positions across all symbols, not only this symbol.
        open_positions = await positions_loader(None)

        if not isinstance(open_positions, (list, tuple)):
            raise ValueError("Open-position snapshot is invalid")

        regime_name = str(regime.get("regime") or "UNKNOWN").strip().upper()
        confluence_quality = str(
            confluence.get("quality") or "LOW"
        ).strip().upper()

        vix = _finite_number(market_data.get("vix", 0))
        if vix is None or vix < 0:
            raise ValueError("VIX value is invalid")

        strength = _finite_number(signal_strength)
        if strength is None:
            raise ValueError("Signal strength is invalid")

        risk_result = assessor(
            capital=settings.default_capital,
            entry_price=entry,
            stop_loss_price=stop,
            rr_ratio=rr,
            option_rr_ratio=option_rr,
            market_regime=regime_name,
            vix=vix,
            days_to_expiry=dte,
            confluence_quality=confluence_quality,
            signal_strength=int(strength),
            open_positions=len(open_positions),
            daily_pnl=daily_pnl,
            oi=oi,
            volume=volume,
            lot_size=int(size),
        )

    except Exception as exc:
        assessment_error = exc

    authority = build_entry_risk_authority(
        action=normalized_action,
        risk_assessment=risk_result,
        assessment_error=assessment_error,
        selected_contract=contract,
        trade_levels=trade_levels,
        lot_size=lot_size,
    )

    approved = is_entry_risk_approved(authority)

    return {
        "risk_assessment": risk_result,
        "entry_risk_authority": authority,
        "risk_approved": approved,
        "assessment_error": (
            str(assessment_error) if assessment_error is not None else None
        ),
    }
