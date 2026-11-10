import pytest
from fastapi import HTTPException

from app.services import paper_trading


def _trade_data(**overrides):
    data = {
        "symbol": "BANKNIFTY",
        "strike": 50000,
        "option_type": "CE",
        "side": "BUY",
        "entry_price": 100.0,
        "stop_loss": 80.0,
        "target_1": 120.0,
        "target_2": 140.0,
        "lots": 2,
        "lot_size": 30,
        "quantity": 60,
    }
    data.update(overrides)
    return data


class FakeSession:
    def __init__(self):
        self.trade = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, trade):
        self.trade = trade

    async def commit(self):
        pass

    async def refresh(self, trade):
        trade.id = "paper-test-id"


@pytest.fixture
def fake_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(
        paper_trading, "AsyncSessionLocal", lambda: session
    )
    return session


@pytest.mark.asyncio
async def test_paper_trade_uses_explicit_symbol_lot_size_and_matching_quantity(
    fake_session,
):
    result = await paper_trading.open_paper_trade(_trade_data())

    assert result["symbol"] == "BANKNIFTY"
    assert result["lots"] == 2
    assert result["quantity"] == 60
    assert fake_session.trade.quantity == 60


@pytest.mark.asyncio
async def test_paper_trade_computes_quantity_server_side_when_omitted(
    monkeypatch, fake_session,
):
    monkeypatch.setattr(
        paper_trading, "resolve_lot_size",
        lambda symbol, chain=None: 65,
    )
    data = _trade_data(
        symbol="NIFTY",
        lots=3,
        lot_size=None,
        quantity=None,
    )

    result = await paper_trading.open_paper_trade(data)

    assert result["lots"] == 3
    assert result["quantity"] == 195
    assert fake_session.trade.quantity == 195


@pytest.mark.asyncio
async def test_paper_trade_rejects_quantity_lot_size_mismatch_before_database(
    monkeypatch,
):
    def forbidden_session():
        raise AssertionError("Database session must not open for invalid sizing")

    monkeypatch.setattr(
        paper_trading, "AsyncSessionLocal", forbidden_session
    )

    with pytest.raises(ValueError, match="quantity must equal"):
        await paper_trading.open_paper_trade(
            _trade_data(quantity=65)
        )


@pytest.mark.asyncio
async def test_paper_trade_rejects_nonpositive_explicit_lot_size_before_database(
    monkeypatch,
):
    def forbidden_session():
        raise AssertionError("Database session must not open for invalid sizing")

    monkeypatch.setattr(
        paper_trading, "AsyncSessionLocal", forbidden_session
    )

    with pytest.raises(ValueError, match="lot_size"):
        await paper_trading.open_paper_trade(
            _trade_data(lot_size=0, quantity=None)
        )


@pytest.mark.asyncio
async def test_paper_trade_rejects_unknown_symbol_without_resolvable_lot_size(
    monkeypatch,
):
    monkeypatch.setattr(
        paper_trading, "resolve_lot_size",
        lambda symbol, chain=None: 0,
    )

    def forbidden_session():
        raise AssertionError("Database session must not open for unknown sizing")

    monkeypatch.setattr(
        paper_trading, "AsyncSessionLocal", forbidden_session
    )

    with pytest.raises(ValueError, match="lot_size"):
        await paper_trading.open_paper_trade(
            _trade_data(
                symbol="UNKNOWN",
                lot_size=None,
                quantity=None,
            )
        )


def test_open_trade_api_model_preserves_lot_size():
    from app.api.routes.paper_trade import OpenTradeRequest

    request = OpenTradeRequest(
        symbol="BANKNIFTY",
        strike=50000,
        option_type="CE",
        entry_price=100,
        stop_loss=80,
        target_1=120,
        target_2=140,
        lots=2,
        quantity=60,
        lot_size=30,
    )

    assert request.model_dump()["lot_size"] == 30


@pytest.mark.asyncio
async def test_open_trade_route_maps_sizing_validation_to_http_422(monkeypatch):
    import app.api.routes.paper_trade as route

    async def invalid_open(data):
        raise ValueError("quantity must equal lots * lot_size")

    monkeypatch.setattr(route, "open_paper_trade", invalid_open)

    request = route.OpenTradeRequest(
        symbol="BANKNIFTY",
        strike=50000,
        option_type="CE",
        entry_price=100,
        stop_loss=80,
        target_1=120,
        target_2=140,
        lots=2,
        quantity=65,
        lot_size=30,
    )

    with pytest.raises(HTTPException) as exc:
        await route.open_trade(request)

    assert exc.value.status_code == 422
