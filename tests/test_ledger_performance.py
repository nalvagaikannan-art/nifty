from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.ledger_performance import (
    _action,
    _trade_return,
    _pnl_per_lot,
    _episode_side,
    _independent_episode_rows,
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


def _row(action, lifecycle, minute, base=None):
    ts = base or datetime(2026, 10, 7, 9, 15)
    return SimpleNamespace(
        action=action,
        lifecycle=lifecycle,
        timestamp=ts + timedelta(minutes=minute),
        market_snapshot_timestamp=None,
    )


def test_episode_side_requires_action_lifecycle_alignment():
    assert _episode_side(_row("BUY CE", "HOLD_CALL", 0)) == "CALL"
    assert _episode_side(_row("SELL PE", "CONFIRMED_CALL", 0)) == "CALL"
    assert _episode_side(_row("BUY PE", "HOLD_PUT", 0)) == "PUT"
    assert _episode_side(_row("SELL CE", "CONFIRMED_PUT", 0)) == "PUT"

    assert _episode_side(_row("BUY CE", "HOLD_PUT", 0)) is None
    assert _episode_side(_row("SELL PE", "CONFIRMED_PUT", 0)) is None
    assert _episode_side(_row("BUY CE", "UNKNOWN", 0)) is None


def test_independent_episode_rows_keep_first_same_side_after_10m_gap():
    base = datetime(2026, 10, 7, 9, 15)

    rows = [
        _row("BUY CE", "HOLD_CALL", 0, base),
        _row("BUY CE", "HOLD_CALL", 5, base),
        _row("BUY CE", "HOLD_CALL", 10, base),
        _row("BUY CE", "HOLD_CALL", 11, base),
        _row("SELL PE", "HOLD_CALL", 12, base),
        _row("BUY CE", "HOLD_CALL", 23, base),
    ]

    selected = _independent_episode_rows(rows)

    assert [r.timestamp for r in selected] == [
        rows[0].timestamp,
        rows[5].timestamp,
    ]


def test_independent_episode_rows_ignore_non_actionable_rows():
    base = datetime(2026, 10, 7, 9, 15)

    rows = [
        _row("WAIT", "UNKNOWN", 0, base),
        _row("BUY CE", "HOLD_CALL", 1, base),
        _row("BUY CE", "HOLD_CALL", 4, base),
        _row("BUY PE", "HOLD_PUT", 20, base),
        _row("BUY PE", "HOLD_PUT", 25, base),
    ]

    selected = _independent_episode_rows(rows)

    assert selected == [rows[1], rows[3]]
