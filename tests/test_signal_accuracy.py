from datetime import datetime, timedelta

from app.services.signal_accuracy import (
    _asof_price,
    _asof_spot_price_with_fallback,
    _target_within_nse_session,
)


def test_asof_price_returns_latest_snapshot_at_or_before_target():
    base = datetime(2026, 9, 3, 10, 0, 0)

    prices = [
        (base, 100.0),
        (base + timedelta(minutes=2), 102.0),
        (base + timedelta(minutes=4), 104.0),
    ]
    timestamps = [ts for ts, _ in prices]

    target = base + timedelta(minutes=5)

    assert _asof_price(prices, timestamps, target, 4) == 104.0


def test_asof_price_returns_exact_snapshot():
    base = datetime(2026, 9, 3, 10, 0, 0)

    prices = [
        (base, 100.0),
        (base + timedelta(minutes=5), 105.0),
    ]
    timestamps = [ts for ts, _ in prices]

    target = base + timedelta(minutes=5)

    assert _asof_price(prices, timestamps, target, 4) == 105.0


def test_asof_price_never_uses_future_snapshot():
    base = datetime(2026, 9, 3, 10, 0, 0)

    prices = [
        (base + timedelta(minutes=5), 105.0),
    ]
    timestamps = [ts for ts, _ in prices]

    target = base + timedelta(minutes=3)

    assert _asof_price(prices, timestamps, target, 4) is None


def test_asof_price_rejects_snapshot_outside_tolerance():
    base = datetime(2026, 9, 3, 10, 0, 0)

    prices = [
        (base, 100.0),
    ]
    timestamps = [ts for ts, _ in prices]

    target = base + timedelta(minutes=5)

    assert _asof_price(prices, timestamps, target, 4) is None

def test_session_horizon_accepts_target_inside_same_session():
    # Naive timestamps are UTC: 14:25 IST -> 15:25 IST.
    event = datetime(2026, 9, 28, 8, 55, 0)
    target = event + timedelta(minutes=60)
    assert _target_within_nse_session(event, target) is True


def test_session_horizon_rejects_target_after_market_close():
    # 14:40 IST -> 15:40 IST crosses the regular-session close.
    event = datetime(2026, 9, 28, 9, 10, 0)
    target = event + timedelta(minutes=60)
    assert _target_within_nse_session(event, target) is False


def test_session_horizon_allows_exact_session_close():
    # 14:30 IST -> exactly 15:30 IST.
    event = datetime(2026, 9, 28, 9, 0, 0)
    target = event + timedelta(minutes=60)
    assert _target_within_nse_session(event, target) is True


def test_session_horizon_rejects_event_before_regular_open():
    # 09:05 IST is before the NSE regular session.
    event = datetime(2026, 9, 28, 3, 35, 0)
    target = event + timedelta(minutes=5)
    assert _target_within_nse_session(event, target) is False


def test_direction_premium_disagreement_counts_both_flat_episode():
    from app.services.signal_accuracy import (
        _record_direction_premium_disagreement,
        _disagreement_comparable_count,
    )

    keys = (
        "both_correct",
        "both_wrong",
        "spot_correct_premium_wrong",
        "spot_wrong_premium_correct",
        "spot_graded_premium_flat",
        "spot_flat_premium_graded",
        "both_flat",
        "missing",
    )
    bucket = {key: 0 for key in keys}
    cases = [
        ("correct", "correct"),
        ("wrong", "wrong"),
        ("correct", "wrong"),
        ("wrong", "correct"),
        ("correct", "flat"),
        ("flat", "wrong"),
        ("flat", "flat"),
        (None, "correct"),
    ]

    for spot_grade, premium_grade in cases:
        _record_direction_premium_disagreement(
            bucket, spot_grade, premium_grade
        )

    assert bucket["both_flat"] == 1
    assert bucket["missing"] == 1
    comparable = _disagreement_comparable_count(bucket)
    assert comparable == 7
    assert comparable + bucket["missing"] == len(cases)

def test_premium_data_gap_diagnostics_count_reason_and_action():
    from app.services.signal_accuracy import _record_premium_data_gap

    reasons = {
        "missing_contract_series": 0,
        "no_entry_snapshot": 0,
        "entry_snapshot_invalid_or_stale": 0,
        "missing_horizon_snapshot": 0,
    }
    by_action = {"CALL_BUY": {}, "PUT_BUY": {}}

    _record_premium_data_gap(
        reasons, by_action, "missing_contract_series", "CALL_BUY"
    )
    _record_premium_data_gap(
        reasons, by_action, "no_entry_snapshot", "CALL_BUY"
    )
    _record_premium_data_gap(
        reasons, by_action, "missing_horizon_snapshot", "PUT_BUY"
    )

    assert sum(reasons.values()) == 3
    assert reasons["missing_contract_series"] == 1
    assert reasons["no_entry_snapshot"] == 1
    assert reasons["missing_horizon_snapshot"] == 1
    assert by_action["CALL_BUY"]["missing_contract_series"] == 1
    assert by_action["CALL_BUY"]["no_entry_snapshot"] == 1
    assert by_action["PUT_BUY"]["missing_horizon_snapshot"] == 1

def test_premium_horizon_session_gate_matches_nse_boundary():
    from datetime import datetime, timedelta
    from app.services.signal_accuracy import _target_within_nse_session

    # Naive timestamps here represent UTC, consistent with the helper.
    # Friday 14:30 IST + 60 minutes lands exactly at the 15:30 close.
    event_at_1430_ist = datetime(2026, 10, 9, 9, 0)
    target_at_close = event_at_1430_ist + timedelta(minutes=60)
    assert _target_within_nse_session(
        event_at_1430_ist, target_at_close
    ) is True

    # Friday 14:31 IST + 60 minutes crosses the regular-session close.
    event_at_1431_ist = datetime(2026, 10, 9, 9, 1)
    target_after_close = event_at_1431_ist + timedelta(minutes=60)
    assert _target_within_nse_session(
        event_at_1431_ist, target_after_close
    ) is False

def test_premium_horizon_gap_example_reports_snapshot_times():
    from datetime import datetime, timedelta
    from app.services.signal_accuracy import _premium_horizon_gap_example

    before = datetime(2026, 10, 9, 9, 0)
    target = datetime(2026, 10, 9, 9, 10)
    after = datetime(2026, 10, 9, 9, 20)

    detail = _premium_horizon_gap_example(
        "CALL_BUY", "27OCT2026", 25000, "CE",
        target, [before, after],
    )

    assert detail["snapshot_before_target"] == before.isoformat()
    assert detail["minutes_since_snapshot_before_target"] == 10.0
    assert detail["snapshot_after_target"] == after.isoformat()
    assert detail["minutes_until_snapshot_after_target"] == 10.0
    assert detail["last_contract_snapshot"] == after.isoformat()

def test_accuracy_indicator_schema_allows_null_success_rate_for_insufficient_data():
    from typing import get_args
    from pydantic import TypeAdapter
    from app.schemas import AccuracyIndicatorsResponse

    item_annotation = AccuracyIndicatorsResponse.model_fields["indicators"].annotation
    item_model = next(
        (
            candidate for candidate in get_args(item_annotation)
            if hasattr(candidate, "model_fields")
            and "success_rate" in candidate.model_fields
        ),
        None,
    )
    assert item_model is not None

    rate_annotation = item_model.model_fields["success_rate"].annotation
    assert TypeAdapter(rate_annotation).validate_python(None) is None


def test_spot_fallback_prefers_market_snapshot():
    from types import SimpleNamespace

    base = datetime(2026, 10, 6, 4, 0)
    prices = [(base, 100.0)]
    timestamps = [base]
    candles = [
        SimpleNamespace(
            timestamp=base - timedelta(minutes=5), close=90.0
        )
    ]

    assert _asof_spot_price_with_fallback(
        prices, timestamps, base + timedelta(minutes=2), 4, candles
    ) == 100.0


def test_spot_fallback_uses_completed_candle_for_missing_or_stale_data():
    from types import SimpleNamespace

    base = datetime(2026, 10, 6, 4, 0)
    candle = SimpleNamespace(timestamp=base, close=104.0)

    assert _asof_spot_price_with_fallback(
        [], [], base + timedelta(minutes=7), 4, [candle]
    ) == 104.0

    stale_prices = [(base, 99.0)]
    stale_candle = SimpleNamespace(
        timestamp=base + timedelta(minutes=5), close=105.0
    )

    assert _asof_spot_price_with_fallback(
        stale_prices,
        [base],
        base + timedelta(minutes=10),
        4,
        [stale_candle],
    ) == 105.0


def test_spot_fallback_never_uses_incomplete_future_candle():
    from types import SimpleNamespace

    base = datetime(2026, 10, 6, 4, 0)
    future_candle = SimpleNamespace(
        timestamp=base + timedelta(minutes=5), close=110.0
    )

    assert _asof_spot_price_with_fallback(
        [], [], base + timedelta(minutes=7), 4, [future_candle]
    ) is None


def test_grade_premium_gross_and_cost_boundaries():
    from app.services.signal_accuracy import _grade_premium

    assert _grade_premium(4.0) == "correct"
    assert _grade_premium(-4.0) == "wrong"
    assert _grade_premium(3.0) == "flat"
    assert _grade_premium(-3.0) == "flat"


def test_grade_premium_cost_can_reduce_favourable_grade():
    from app.services.signal_accuracy import _grade_premium

    assert _grade_premium(4.0, 2.0) == "flat"
    assert _grade_premium(6.0, 2.0) == "correct"


def test_grade_premium_cost_worsens_unfavourable_move():
    from app.services.signal_accuracy import _grade_premium

    assert _grade_premium(-4.0, 2.0) == "wrong"


def test_grade_premium_zero_move_is_flat():
    from app.services.signal_accuracy import _grade_premium

    assert _grade_premium(0.0) == "flat"


def _premium_accuracy_result(
    *,
    version=2,
    fresh=True,
    action="BUY CE",
    symbol="NIFTY",
):
    from app.services.entry_risk_authority import build_entry_risk_authority

    option_type = action.split()[1]
    entry = 100.0
    stop = 80.0
    lot_size = 65
    risk_per_lot = (entry - stop) * lot_size

    authority = build_entry_risk_authority(
        action=action,
        risk_assessment={
            "allowed": True,
            "lots": 1,
            "quantity": lot_size,
            "max_loss": risk_per_lot,
            "max_risk_amount": risk_per_lot,
            "risk_per_lot": risk_per_lot,
            "no_trade_reasons": [],
            "reasons": ["Controlled test approval"],
            "warnings": [],
        },
        selected_contract={
            "symbol": symbol,
            "strike": 25000,
            "type": option_type,
            "expiry": "15DEC2026",
            "entry_price": entry,
            "sl": stop,
        },
        trade_levels={
            "option_entry": entry,
            "option_sl": stop,
            "option_rr_ratio": 2.0,
        },
        lot_size=lot_size,
    )

    result = {
        "symbol": symbol,
        "metadata_version": version,
        "fresh_entry_allowed": fresh,
        "signal_action": action,
        "best_strategy": action,
        "preferred_side": "CALL" if option_type == "CE" else "PUT",
        "signal_lifecycle": (
            "CONFIRMED_CALL" if option_type == "CE" else "CONFIRMED_PUT"
        ),
        "signal_active_side": "CALL" if option_type == "CE" else "PUT",
        "recommended_option": {
            "available": True,
            "strike": 25000,
            "type": option_type,
            "expiry": "15DEC2026",
            "entry_price": entry,
            "sl": stop,
        },
    }

    if version == 2:
        result["entry_risk_authority"] = authority

    return result


def test_premium_accuracy_requires_matching_v2_risk_authority():
    from app.services.signal_accuracy import _premium_risk_classification

    result = _premium_accuracy_result()
    assert _premium_risk_classification(result, "CALL_BUY") == "RISK_APPROVED"

    result["entry_risk_authority"]["action"] = "BUY PE"
    assert _premium_risk_classification(result, "CALL_BUY") == "UNAPPROVED"


def test_premium_accuracy_rejects_contract_identity_mismatch():
    from app.services.signal_accuracy import _premium_risk_classification

    result = _premium_accuracy_result()
    result["recommended_option"]["strike"] = 25100
    assert _premium_risk_classification(result, "CALL_BUY") == "UNAPPROVED"

    result = _premium_accuracy_result()
    result["recommended_option"]["symbol"] = "BANKNIFTY"
    assert _premium_risk_classification(result, "CALL_BUY") == "UNAPPROVED"


def test_premium_accuracy_separates_v1_and_unversioned_legacy_rows():
    from app.services.signal_accuracy import _premium_risk_classification

    legacy_v1 = _premium_accuracy_result(version=1, fresh=True)
    unversioned = _premium_accuracy_result(version=None, fresh=True)
    denied_v1 = _premium_accuracy_result(version=1, fresh=False)

    assert (
        _premium_risk_classification(legacy_v1, "CALL_BUY")
        == "LEGACY_RISK_UNVERIFIED"
    )
    assert (
        _premium_risk_classification(unversioned, "CALL_BUY")
        == "LEGACY_RISK_UNVERIFIED"
    )
    assert (
        _premium_risk_classification(denied_v1, "CALL_BUY")
        == "UNAPPROVED"
    )


def test_premium_accuracy_rejects_v2_without_fresh_approval():
    from app.services.signal_accuracy import _premium_risk_classification

    result = _premium_accuracy_result(version=2, fresh=False)
    assert _premium_risk_classification(result, "CALL_BUY") == "UNAPPROVED"

    result = _premium_accuracy_result()
    result.pop("entry_risk_authority")
    assert _premium_risk_classification(result, "CALL_BUY") == "UNAPPROVED"


def test_premium_accuracy_episode_selection_keeps_legacy_separate_from_v2():
    from types import SimpleNamespace
    from app.services.signal_accuracy import _select_premium_accuracy_episodes

    base = datetime(2026, 10, 27, 4, 0)

    def row(minutes, result):
        ts = base + timedelta(minutes=minutes)
        result = {
            **result,
            "market_snapshot_timestamp": ts.isoformat(),
        }
        return SimpleNamespace(timestamp=ts, result=result)

    rows = [
        row(0, _premium_accuracy_result(version=1, fresh=True)),
        row(5, _premium_accuracy_result(version=1, fresh=True)),
        # Different authority class creates a separate episode even inside
        # the same 10-minute direction/action window.
        row(6, _premium_accuracy_result(version=2, fresh=True)),
        row(8, _premium_accuracy_result(version=2, fresh=True)),
        row(30, _premium_accuracy_result(version=2, fresh=False)),
    ]

    approved, legacy_count, unapproved_count = (
        _select_premium_accuracy_episodes(rows)
    )

    assert len(approved) == 1
    assert legacy_count == 1
    assert unapproved_count == 1


def test_premium_accuracy_only_approves_supported_buy_action():
    from app.services.signal_accuracy import _premium_risk_classification

    result = _premium_accuracy_result(action="SELL CE")
    assert _premium_risk_classification(result, "CALL_SELL") == "UNAPPROVED"
