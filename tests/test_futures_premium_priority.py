import pytest

from app.services.angel_one import (
    ANGEL_PRIORITY_BACKGROUND,
    ANGEL_PRIORITY_NORMAL,
    get_angel_call_priority,
    reset_angel_call_priority,
    set_angel_call_priority,
)
from app.services.data_fetcher import DataFetcher


@pytest.mark.asyncio
async def test_futures_premium_refresh_uses_background_priority(monkeypatch):
    fetcher = DataFetcher()
    observed = []

    async def fake_fetch(symbol):
        observed.append((symbol, get_angel_call_priority()))
        return {
            "status": "unavailable",
            "premium": 0.0,
            "premium_pct": 0.0,
        }

    monkeypatch.setattr(fetcher, "_fetch_futures_premium_live", fake_fetch)

    caller_token = set_angel_call_priority(ANGEL_PRIORITY_NORMAL)
    try:
        await fetcher._refresh_futures_premium_background("SENSEX")

        assert observed == [("SENSEX", ANGEL_PRIORITY_BACKGROUND)]
        # The background task must restore its caller's priority.
        assert get_angel_call_priority() == ANGEL_PRIORITY_NORMAL
    finally:
        reset_angel_call_priority(caller_token)
