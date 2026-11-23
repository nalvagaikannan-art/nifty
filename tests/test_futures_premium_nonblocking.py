import asyncio
import time

import pytest

from app.services.data_fetcher import DataFetcher


def live_value():
    return {
        "status": "live",
        "premium": -18.5,
        "premium_pct": -0.07,
        "futures_ltp": 22742.0,
        "futures_expiry": "27OCT2026",
    }


def fetcher_with_cache(monkeypatch, age_seconds):
    fetcher = DataFetcher()
    value = live_value()
    fetcher._futures_premium_cache["NIFTY"] = value
    fetcher._futures_premium_cache_ts["NIFTY"] = time.time() - age_seconds

    scheduled = []
    monkeypatch.setattr(
        fetcher,
        "_schedule_futures_premium_refresh",
        lambda symbol: scheduled.append(symbol),
    )
    return fetcher, value, scheduled


@pytest.mark.asyncio
async def test_fresh_cache_returns_without_refresh(monkeypatch):
    fetcher, value, scheduled = fetcher_with_cache(monkeypatch, 5)

    result = await fetcher.get_futures_premium("NIFTY")

    assert result == value
    assert scheduled == []


@pytest.mark.asyncio
async def test_soft_stale_cache_returns_and_schedules_refresh(monkeypatch):
    fetcher, value, scheduled = fetcher_with_cache(monkeypatch, 30)

    result = await fetcher.get_futures_premium("NIFTY")

    assert result == value
    assert result["status"] == "live"
    assert scheduled == ["NIFTY"]


@pytest.mark.asyncio
async def test_hard_stale_cache_does_not_wait_for_lock(monkeypatch):
    fetcher, value, scheduled = fetcher_with_cache(monkeypatch, 90)
    await fetcher._futures_premium_lock.acquire()

    try:
        result = await asyncio.wait_for(
            fetcher.get_futures_premium("NIFTY"),
            timeout=0.2,
        )
    finally:
        fetcher._futures_premium_lock.release()

    assert result["status"] == "stale"
    assert result["premium"] == value["premium"]
    assert result["stale_age_sec"] >= 60
    assert scheduled == ["NIFTY"]


@pytest.mark.asyncio
async def test_cold_cache_does_not_wait_for_lock(monkeypatch):
    fetcher = DataFetcher()
    scheduled = []
    monkeypatch.setattr(
        fetcher,
        "_schedule_futures_premium_refresh",
        lambda symbol: scheduled.append(symbol),
    )
    await fetcher._futures_premium_lock.acquire()

    try:
        result = await asyncio.wait_for(
            fetcher.get_futures_premium("FINNIFTY"),
            timeout=0.2,
        )
    finally:
        fetcher._futures_premium_lock.release()

    assert result["status"] == "unavailable"
    assert result["premium"] == 0.0
    assert scheduled == ["FINNIFTY"]
