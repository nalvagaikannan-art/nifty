from datetime import datetime, timedelta

from app.services.signal_accuracy import _asof_price


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
