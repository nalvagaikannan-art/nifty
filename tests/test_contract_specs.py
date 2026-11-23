import pytest

from app.services.contract_specs import resolve_lot_size


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("NIFTY", 65),
        ("BANKNIFTY", 30),
        ("FINNIFTY", 60),
        ("SENSEX", 20),
    ],
)
def test_current_contract_lot_size_fallbacks(symbol, expected):
    assert resolve_lot_size(symbol) == expected


def test_chain_lot_size_overrides_symbol_fallback():
    assert resolve_lot_size("SENSEX", {"lot_size": 25}) == 25


def test_unknown_symbol_without_chain_metadata_returns_zero():
    assert resolve_lot_size("UNKNOWN") == 0
