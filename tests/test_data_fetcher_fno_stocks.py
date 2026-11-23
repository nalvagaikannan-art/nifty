"""DataFetcher F&O-stock routing tests; no live broker/network access."""
import pytest
from unittest.mock import AsyncMock, patch

from app.exceptions import MarketDataError
from app.services.data_fetcher import DataFetcher


@pytest.mark.asyncio
async def test_supported_market_symbol_accepts_core_indices_without_broker():
    fetcher = DataFetcher()

    assert await fetcher.is_supported_market_symbol("NIFTY")
    assert await fetcher.is_supported_market_symbol("sensex")


@pytest.mark.asyncio
async def test_supported_market_symbol_rejects_stock_when_instrument_master_unavailable():
    fetcher = DataFetcher()

    with patch(
        "app.services.angel_one.angel_session.is_configured",
        False,
    ):
        assert not await fetcher.is_supported_market_symbol("RELIANCE")


@pytest.mark.asyncio
async def test_stock_option_chain_fails_closed_without_angel_chain():
    fetcher = DataFetcher()
    with (
        patch.object(fetcher, "is_supported_market_symbol", new=AsyncMock(return_value=True)),
        patch.object(fetcher, "get_spot", new=AsyncMock(return_value={
            "price": 2500.0, "data_source": "angel_one"
        })),
        patch.object(fetcher, "_try_angel_option_chain", new=AsyncMock(return_value=None)),
        patch.object(fetcher, "_get", new=AsyncMock()) as nse_get,
    ):
        with pytest.raises(MarketDataError, match="stock option chain unavailable"):
            await fetcher._get_option_chain_cached("RELIANCE", "31DEC2099")
        nse_get.assert_not_awaited()
