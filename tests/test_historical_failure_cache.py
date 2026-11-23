import asyncio

import pytest

from app.services.data_fetcher import DataFetcher
from app.utils import cache as cache_module


@pytest.mark.parametrize("nse_mode", ["empty", "exception"])
def test_terminal_historical_failure_is_short_cached(monkeypatch, nse_mode):
    # async_cache deliberately excludes self from its key.
    cache_key = (
        "DataFetcher._get_historical_prices_base:"
        "(('symbol', \"'BANKNIFTY\'\"), ('days', '73'))"
    )
    previous_entry = cache_module._cache.pop(cache_key, None)

    async def scenario():
        fetcher = DataFetcher()
        calls = {"angel": 0, "zerodha": 0, "nse": 0}

        async def angel(symbol, days, include_volume_proxy=True):
            calls["angel"] += 1
            return None

        async def zerodha(symbol, days):
            calls["zerodha"] += 1
            return None

        async def nse(endpoint, params=None):
            calls["nse"] += 1
            if nse_mode == "exception":
                raise RuntimeError("simulated provider failure")
            return {"data": []}

        monkeypatch.setattr(fetcher, "_try_angel_historical", angel)
        monkeypatch.setattr(fetcher, "_try_zerodha_historical", zerodha)
        monkeypatch.setattr(fetcher, "_get", nse)

        first = await fetcher.get_historical_prices(
            "BANKNIFTY", days=73, include_volume_proxy=False
        )
        second = await fetcher.get_historical_prices(
            "BANKNIFTY", days=73, include_volume_proxy=False
        )

        assert first == second
        assert first["closes"] == []
        assert first["volumes"] == []
        assert first["available"] is False
        assert first["data_source"] == "unavailable"
        assert calls == {"angel": 1, "zerodha": 1, "nse": 1}

        entry = cache_module._cache.get(cache_key)
        assert entry is not None
        assert len(entry) == 3
        assert entry[2] == 60

    try:
        asyncio.run(scenario())
    finally:
        cache_module._cache.pop(cache_key, None)
        if previous_entry is not None:
            cache_module._cache[cache_key] = previous_entry


def test_sensex_historical_failure_is_short_cached(monkeypatch):
    expected_key = (
        "DataFetcher._get_historical_prices_base:"
        + repr((("symbol", repr("SENSEX")), ("days", repr(74))))
    )
    previous_entry = cache_module._cache.pop(expected_key, None)

    async def scenario():
        fetcher = DataFetcher()
        calls = {"angel": 0, "zerodha": 0, "nse": 0}

        async def angel(symbol, days, include_volume_proxy=True):
            calls["angel"] += 1
            return None

        async def zerodha(symbol, days):
            calls["zerodha"] += 1
            return None

        async def nse(*args, **kwargs):
            calls["nse"] += 1
            raise AssertionError("SENSEX must not call NSE historical fallback")

        monkeypatch.setattr(fetcher, "_try_angel_historical", angel)
        monkeypatch.setattr(fetcher, "_try_zerodha_historical", zerodha)
        monkeypatch.setattr(fetcher, "_get", nse)

        first = await fetcher.get_historical_prices(
            "SENSEX", days=74, include_volume_proxy=False
        )
        second = await fetcher.get_historical_prices(
            "SENSEX", days=74, include_volume_proxy=False
        )

        assert first == second
        assert first["closes"] == []
        assert first["volumes"] == []
        assert first["available"] is False
        assert first["data_source"] == "unavailable"
        assert "SENSEX" in first["reason"]
        assert calls == {"angel": 1, "zerodha": 1, "nse": 0}

        entry = cache_module._cache.get(expected_key)
        assert entry is not None
        assert len(entry) == 3
        assert entry[2] == 60

    try:
        asyncio.run(scenario())
    finally:
        cache_module._cache.pop(expected_key, None)
        if previous_entry is not None:
            cache_module._cache[expected_key] = previous_entry
