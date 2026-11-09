from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.call_tracker import (
    _entry_classification,
    _evaluate_quote_path,
    _saved_level_display,
    _validated_levels,
)


def _row(action, lifecycle, allowed, snapshot=None):
    from app.services.entry_risk_authority import build_entry_risk_authority

    decision = {
        "metadata_version": 1,
        "fresh_entry_allowed": allowed,
        "lifecycle": lifecycle,
        "signal_action": action,
    }

    if allowed is True and action in ("BUY CE", "BUY PE"):
        stop = 65.0
        size = 65
        risk_per_lot = (100.0 - stop) * size
        authority = build_entry_risk_authority(
            action=action,
            risk_assessment={
                "allowed": True,
                "lots": 1,
                "quantity": size,
                "max_loss": risk_per_lot,
                "max_risk_amount": risk_per_lot,
                "risk_per_lot": risk_per_lot,
                "no_trade_reasons": [],
                "reasons": ["Test approval"],
                "warnings": [],
            },
            selected_contract={
                "symbol": "NIFTY",
                "expiry": "29OCT2026",
                "strike": 25000,
                "type": action.split()[1],
                "entry_price": 100.0,
                "sl": stop,
            },
            trade_levels={
                "option_entry": 100.0,
                "option_sl": stop,
                "option_rr_ratio": 2.0,
            },
            lot_size=size,
        )
        decision["metadata_version"] = 2
        decision["entry_risk_authority"] = authority

    return SimpleNamespace(
        action=action,
        lifecycle=lifecycle,
        entry_price=100.0,
        entry_snapshot={
            "entry_price": 100.0,
            "sl": 135.0 if action.startswith("SELL") else 65.0,
            "t1": 65.0 if action.startswith("SELL") else 120.0,
            "t2": 50.0 if action.startswith("SELL") else 140.0,
            "t3": 35.0 if action.startswith("SELL") else 160.0,
            "strategy_decision": decision,
            **(snapshot or {}),
        },
    )



def test_fresh_entry_classification_separates_hold_and_denied():
    assert _entry_classification(
        _row("BUY CE", "CONFIRMED_CALL", True)
    ) == "FRESH_ENTRY_ALLOWED"

    assert _entry_classification(
        _row("BUY CE", "HOLD_CALL", False)
    ) == "HOLD_EXISTING_DIRECTION"

    assert _entry_classification(
        _row("BUY CE", "CONFIRMED_CALL", False)
    ) == "ENTRY_DENIED"


def test_legacy_v1_positive_entry_is_not_risk_approved():
    row = _row("SELL PE", "CONFIRMED_CALL", True)
    assert row.entry_snapshot["strategy_decision"]["metadata_version"] == 1
    assert _entry_classification(row) == "LEGACY_RISK_UNVERIFIED"


def test_old_or_unversioned_entry_metadata_stays_unknown():
    row = _row("BUY CE", "CONFIRMED_CALL", True)
    row.entry_snapshot["strategy_decision"]["metadata_version"] = 0
    assert _entry_classification(row) == "ENTRY_AUTHORITY_UNKNOWN"


def test_fresh_allowed_hold_conflict_is_not_counted_as_a_new_call():
    assert _entry_classification(
        _row("SELL PE", "HOLD_CALL", True)
    ) == "ENTRY_AUTHORITY_INCONSISTENT"


def test_buy_and_sell_levels_require_opposite_price_ordering():
    buy = _row("BUY CE", "CONFIRMED_CALL", True)
    sell = _row("SELL PE", "CONFIRMED_CALL", True)
    assert _validated_levels(buy)[0] is not None
    assert _validated_levels(sell)[0] is not None

    sell.entry_snapshot["sl"] = 90.0
    assert _validated_levels(sell)[0] is None


def test_sell_stop_first_ignores_later_target_for_before_stop_count():
    start = datetime(2026, 10, 26, 9, 20)
    levels = {"entry": 100, "SL": 135, "T1": 65, "T2": 50, "T3": 35}
    result = _evaluate_quote_path(
        "SELL PE",
        levels,
        [
            (start + timedelta(minutes=1), 137),
            (start + timedelta(minutes=2), 60),
        ],
        start + timedelta(minutes=3),
        start + timedelta(hours=1),
    )
    assert result["first_barrier"] == "STOP_LOSS"
    assert result["status"] == "STOP_LOSS_FIRST"
    assert result["targets_before_stop"] == []
    assert result["targets_after_stop"] == ["T1"]


def test_sell_targets_before_later_stop_are_reported_in_order():
    start = datetime(2026, 10, 26, 9, 20)
    levels = {"entry": 100, "SL": 135, "T1": 65, "T2": 50, "T3": 35}
    result = _evaluate_quote_path(
        "SELL PE",
        levels,
        [
            (start + timedelta(minutes=1), 60),
            (start + timedelta(minutes=2), 49),
            (start + timedelta(minutes=3), 137),
        ],
        start + timedelta(minutes=4),
        start + timedelta(hours=1),
    )
    assert result["first_barrier"] == "T1"
    assert result["targets_before_stop"] == ["T1", "T2"]
    assert result["status"] == "T2_THEN_STOP"


def test_buy_target_uses_rising_premium_and_stop_uses_falling_premium():
    start = datetime(2026, 10, 26, 9, 20)
    levels = {"entry": 100, "SL": 65, "T1": 120, "T2": 140, "T3": 160}
    result = _evaluate_quote_path(
        "BUY CE",
        levels,
        [(start + timedelta(minutes=1), 121)],
        start + timedelta(minutes=2),
        start + timedelta(hours=1),
    )
    assert result["first_barrier"] == "T1"
    assert result["status"] == "T1_REACHED"
    assert result["targets_before_stop"] == ["T1"]


def test_no_level_touch_is_not_mislabeled_as_a_win_or_loss():
    start = datetime(2026, 10, 26, 9, 20)
    levels = {"entry": 100, "SL": 135, "T1": 65, "T2": 50, "T3": 35}
    result = _evaluate_quote_path(
        "SELL PE",
        levels,
        [(start + timedelta(minutes=1), 98)],
        start + timedelta(minutes=2),
        start + timedelta(hours=1),
    )
    assert result["status"] == "MONITORING_NO_LEVEL_TOUCHED"
    assert result["first_barrier"] is None


def test_hold_entries_keep_saved_levels_visible_for_context():
    row = _row("SELL PE", "HOLD_CALL", False)

    assert _entry_classification(row) == "HOLD_EXISTING_DIRECTION"
    assert _saved_level_display(row) == {
        "entry": 100.0,
        "SL": 135.0,
        "T1": 65.0,
        "T2": 50.0,
        "T3": 35.0,
    }
