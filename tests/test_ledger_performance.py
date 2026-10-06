from app.services.ledger_performance import (
    _action,
    _trade_return,
    _pnl_per_lot,
)


def test_ledger_action_normalization():
    assert _action("BUY CE") == "CALL_BUY"
    assert _action("BUY PE") == "PUT_BUY"
    assert _action("SELL CE") == "CALL_SELL"
    assert _action("SELL PE") == "PUT_SELL"


def test_direction_return_is_action_adjusted():
    assert _trade_return("BUY CE", 1.5) == 1.5
    assert _trade_return("SELL PE", 1.5) == 1.5
    assert _trade_return("BUY PE", 1.5) == -1.5
    assert _trade_return("SELL CE", 1.5) == -1.5


def test_option_pnl_is_action_adjusted_per_lot():
    assert _pnl_per_lot(100, 110, 65, "BUY CE") == 650
    assert _pnl_per_lot(110, 100, 65, "SELL PE") == 650
    assert _pnl_per_lot(100, 90, 65, "BUY CE") == -650
    assert _pnl_per_lot(90, 100, 65, "SELL CE") == -650
