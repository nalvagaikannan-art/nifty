import time

import pytest

from app.services.data_fetcher import DataFetcher


@pytest.mark.asyncio
async def test_failed_premium_refresh_respects_per_symbol_cooldown(monkeypatch):
    fetcher = DataFetcher()
    fetcher._futures_premium_refresh_cooldown = 60.0
    calls = []

    async def unavailable_fetch(symbol):
        calls.append(symbol)
        return {
            "status": "unavailable",
            "premium": 0.0,
            "premium_pct": 0.0,
        }

    monkeypatch.setattr(
        fetcher, "_fetch_futures_premium_live", unavailable_fetch
    )

    fetcher._schedule_futures_premium_refresh("FINNIFTY")
    first_task = fetcher._futures_premium_refresh_tasks["FINNIFTY"]
    await first_task

    assert calls == ["FINNIFTY"]
    assert "FINNIFTY" not in fetcher._futures_premium_refresh_tasks

    # A completed unavailable refresh must not be retried immediately.
    fetcher._schedule_futures_premium_refresh("FINNIFTY")
    assert "FINNIFTY" not in fetcher._futures_premium_refresh_tasks
    assert calls == ["FINNIFTY"]

    # Once the per-symbol cooldown expires, another refresh is allowed.
    fetcher._futures_premium_refresh_last_scheduled_ts["FINNIFTY"] = (
        time.monotonic() - fetcher._futures_premium_refresh_cooldown - 1
    )
    fetcher._schedule_futures_premium_refresh("FINNIFTY")
    second_task = fetcher._futures_premium_refresh_tasks["FINNIFTY"]

    assert second_task is not first_task
    await second_task
    assert calls == ["FINNIFTY", "FINNIFTY"]
