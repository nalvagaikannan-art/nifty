from datetime import datetime, timedelta

from app.services.accuracy_engine import (
    _indicator_horizon_in_session,
    _nearest_price,
)


def test_nearest_price_does_not_choose_future_snapshot():
    target = datetime(2026, 10, 14, 10, 0)
    prices = [
        (target - timedelta(minutes=2), 100.0),
        (target + timedelta(minutes=1), 120.0),
    ]
    assert _nearest_price(prices, target, 45) == 100.0


def test_nearest_price_returns_none_when_all_snapshots_are_future():
    target = datetime(2026, 10, 14, 10, 0)
    prices = [(target + timedelta(minutes=1), 120.0)]
    assert _nearest_price(prices, target, 45) is None


def test_nearest_price_rejects_stale_asof_snapshot():
    target = datetime(2026, 10, 14, 10, 0)
    prices = [(target - timedelta(minutes=46), 100.0)]
    assert _nearest_price(prices, target, 45) is None


def test_indicator_horizon_accepts_exact_nse_close():
    assert _indicator_horizon_in_session(datetime(2026, 10, 14, 9, 0)) is True


def test_indicator_horizon_rejects_after_nse_close():
    assert _indicator_horizon_in_session(datetime(2026, 10, 14, 9, 1)) is False
