import pytest

import app.services.entry_risk_service as service
from app.services.entry_risk_authority import is_entry_risk_approved


def _contract():
    return {
        "symbol": "NIFTY",
        "strike": 25000,
        "type": "CE",
        "expiry": "15DEC2026",
        "entry_price": 100.0,
        "sl": 80.0,
        "oi": 1200,
        "volume": 2400,
    }


def _levels():
    return {
        "option_entry": 100.0,
        "option_sl": 80.0,
        "rr_ratio": 2.0,
        "option_rr_ratio": 2.0,
    }


def _approved_verdict(kwargs):
    risk_per_lot = (
        kwargs["entry_price"] - kwargs["stop_loss_price"]
    ) * kwargs["lot_size"]
    return {
        "allowed": True,
        "lots": 1,
        "quantity": kwargs["lot_size"],
        "max_loss": risk_per_lot,
        "max_risk_amount": risk_per_lot + 500,
        "risk_per_lot": risk_per_lot,
        "no_trade_reasons": [],
        "reasons": ["Controlled test verdict"],
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_shared_service_passes_exact_liquidity_and_global_position_count(
    monkeypatch,
):
    monkeypatch.setattr(service, "days_to_expiry", lambda expiry: 5)
    calls = {}

    async def daily_loader():
        calls["daily"] = True
        return -125.0

    async def positions_loader(symbol):
        calls["positions_symbol"] = symbol
        return [{"symbol": "NIFTY"}, {"symbol": "BANKNIFTY"}, {"symbol": "SENSEX"}]

    def assessor(**kwargs):
        calls["risk_kwargs"] = kwargs
        return _approved_verdict(kwargs)

    result = await service.assess_entry_risk_authority(
        action="BUY CE",
        symbol="NIFTY",
        market_data={"symbol": "NIFTY", "vix": 14.0, "option_chain": {"symbol": "NIFTY", "expiry": "15DEC2026"}},
        confluence={"quality": "HIGH"},
        regime={"regime": "TREND_UP"},
        signal_strength=72,
        selected_contract=_contract(),
        trade_levels=_levels(),
        lot_size=65,
        risk_assessor=assessor,
        daily_pnl_loader=daily_loader,
        open_trades_loader=positions_loader,
    )

    assert result["risk_approved"] is True
    assert is_entry_risk_approved(result["entry_risk_authority"])
    assert calls["daily"] is True
    assert calls["positions_symbol"] is None
    assert calls["risk_kwargs"]["open_positions"] == 3
    assert calls["risk_kwargs"]["oi"] == 1200
    assert calls["risk_kwargs"]["volume"] == 2400
    assert calls["risk_kwargs"]["days_to_expiry"] == 5
    frozen = result["entry_risk_authority"]["snapshot"]["selected_contract"]
    assert frozen["oi"] == 1200
    assert frozen["volume"] == 2400


@pytest.mark.asyncio
async def test_unparseable_expiry_fails_closed_before_risk_engine(monkeypatch):
    monkeypatch.setattr(service, "days_to_expiry", lambda expiry: None)
    called = {"risk": False, "daily": False, "positions": False}

    async def daily_loader():
        called["daily"] = True
        return 0

    async def positions_loader(symbol):
        called["positions"] = True
        return []

    def assessor(**kwargs):
        called["risk"] = True
        return _approved_verdict(kwargs)

    result = await service.assess_entry_risk_authority(
        action="BUY CE",
        symbol="NIFTY",
        market_data={"symbol": "NIFTY", "vix": 14.0, "option_chain": {"symbol": "NIFTY", "expiry": "15DEC2026"}},
        confluence={"quality": "HIGH"},
        regime={"regime": "TREND_UP"},
        signal_strength=70,
        selected_contract=_contract(),
        trade_levels=_levels(),
        lot_size=65,
        risk_assessor=assessor,
        daily_pnl_loader=daily_loader,
        open_trades_loader=positions_loader,
    )

    assert result["risk_approved"] is False
    assert result["entry_risk_authority"]["status"] == "ERROR"
    assert "expiry" in " ".join(result["entry_risk_authority"]["reasons"]).lower()
    assert called == {"risk": False, "daily": False, "positions": False}


@pytest.mark.asyncio
async def test_sell_action_remains_unassessed_without_calling_buy_risk_engine():
    async def forbidden(*args, **kwargs):
        raise AssertionError("SELL must not call the BUY risk model")

    result = await service.assess_entry_risk_authority(
        action="SELL CE",
        symbol="NIFTY",
        market_data={},
        confluence={},
        regime={},
        signal_strength=70,
        selected_contract=_contract(),
        trade_levels=_levels(),
        lot_size=65,
        daily_pnl_loader=forbidden,
        open_trades_loader=forbidden,
        risk_assessor=forbidden,
    )

    assert result["risk_approved"] is False
    assert result["entry_risk_authority"]["status"] == "UNASSESSED"


@pytest.mark.asyncio
async def test_missing_liquidity_evidence_fails_closed(monkeypatch):
    monkeypatch.setattr(service, "days_to_expiry", lambda expiry: 5)
    contract = _contract()
    contract.pop("oi")

    result = await service.assess_entry_risk_authority(
        action="BUY CE",
        symbol="NIFTY",
        market_data={"symbol": "NIFTY", "vix": 14.0, "option_chain": {"symbol": "NIFTY", "expiry": "15DEC2026"}},
        confluence={"quality": "HIGH"},
        regime={"regime": "TREND_UP"},
        signal_strength=70,
        selected_contract=contract,
        trade_levels=_levels(),
        lot_size=65,
    )

    assert result["risk_approved"] is False
    assert result["entry_risk_authority"]["status"] == "ERROR"


@pytest.mark.asyncio
async def test_risk_engine_rejection_remains_rejected(monkeypatch):
    monkeypatch.setattr(service, "days_to_expiry", lambda expiry: 5)

    async def daily_loader():
        return 0.0

    async def positions_loader(symbol):
        return []

    def rejected(**kwargs):
        return {
            "allowed": False,
            "lots": 0,
            "quantity": 0,
            "max_loss": 0,
            "max_risk_amount": 0,
            "no_trade_reasons": ["Controlled test rejection"],
            "warnings": [],
        }

    result = await service.assess_entry_risk_authority(
        action="BUY PE",
        symbol="NIFTY",
        market_data={"symbol": "NIFTY", "vix": 14.0, "option_chain": {"symbol": "NIFTY", "expiry": "15DEC2026"}},
        confluence={"quality": "HIGH"},
        regime={"regime": "TREND_DOWN"},
        signal_strength=70,
        selected_contract={
            **_contract(),
            "type": "PE",
        },
        trade_levels=_levels(),
        lot_size=65,
        risk_assessor=rejected,
        daily_pnl_loader=daily_loader,
        open_trades_loader=positions_loader,
    )

    assert result["risk_approved"] is False
    assert result["entry_risk_authority"]["status"] == "REJECTED"

@pytest.mark.parametrize(
    ("symbol", "lot_size"),
    [
        ("NIFTY", 65),
        ("BANKNIFTY", 30),
        ("FINNIFTY", 60),
        ("SENSEX", 20),
    ],
)
@pytest.mark.asyncio
async def test_four_supported_symbols_use_matching_contract_identity_and_lot_size(
    monkeypatch, symbol, lot_size
):
    monkeypatch.setattr(service, "days_to_expiry", lambda expiry: 5)
    calls = {}

    async def daily_loader():
        return 0.0

    async def positions_loader(requested_symbol):
        assert requested_symbol is None
        return []

    def assessor(**kwargs):
        calls.update(kwargs)
        return _approved_verdict(kwargs)

    contract = {**_contract(), "symbol": symbol}
    market_data = {
        "symbol": symbol,
        "vix": 14.0,
        "option_chain": {
            "symbol": symbol,
            "expiry": "15DEC2026",
        },
    }

    result = await service.assess_entry_risk_authority(
        action="BUY CE",
        symbol=symbol,
        market_data=market_data,
        confluence={"quality": "HIGH"},
        regime={"regime": "TREND_UP"},
        signal_strength=72,
        selected_contract=contract,
        trade_levels=_levels(),
        lot_size=lot_size,
        risk_assessor=assessor,
        daily_pnl_loader=daily_loader,
        open_trades_loader=positions_loader,
    )

    assert result["risk_approved"] is True, symbol
    assert is_entry_risk_approved(result["entry_risk_authority"]), symbol
    assert calls["lot_size"] == lot_size, symbol
    assert result["entry_risk_authority"]["snapshot"]["selected_contract"]["symbol"] == symbol
    assert result["entry_risk_authority"]["snapshot"]["lot_size"] == lot_size
    assert result["entry_risk_authority"]["snapshot"]["quantity"] == lot_size


@pytest.mark.parametrize(
    "mismatch_field",
    ["market", "chain", "contract"],
)
@pytest.mark.asyncio
async def test_shared_service_rejects_cross_symbol_identity_before_risk_engine(
    monkeypatch, mismatch_field
):
    monkeypatch.setattr(service, "days_to_expiry", lambda expiry: 5)
    market_data = {
        "symbol": "NIFTY",
        "vix": 14.0,
        "option_chain": {
            "symbol": "NIFTY",
            "expiry": "15DEC2026",
        },
    }
    contract = _contract()

    if mismatch_field == "market":
        market_data["symbol"] = "BANKNIFTY"
    elif mismatch_field == "chain":
        market_data["option_chain"]["symbol"] = "BANKNIFTY"
    else:
        contract["symbol"] = "BANKNIFTY"

    called = {"risk": False, "daily": False, "positions": False}

    async def daily_loader():
        called["daily"] = True
        return 0.0

    async def positions_loader(requested_symbol):
        called["positions"] = True
        return []

    def assessor(**kwargs):
        called["risk"] = True
        return _approved_verdict(kwargs)

    result = await service.assess_entry_risk_authority(
        action="BUY CE",
        symbol="NIFTY",
        market_data=market_data,
        confluence={"quality": "HIGH"},
        regime={"regime": "TREND_UP"},
        signal_strength=72,
        selected_contract=contract,
        trade_levels=_levels(),
        lot_size=65,
        risk_assessor=assessor,
        daily_pnl_loader=daily_loader,
        open_trades_loader=positions_loader,
    )

    assert result["risk_approved"] is False
    assert result["entry_risk_authority"]["status"] == "ERROR"
    assert "symbol" in " ".join(
        result["entry_risk_authority"]["reasons"]
    ).lower()
    assert called == {"risk": False, "daily": False, "positions": False}
