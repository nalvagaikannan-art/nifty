"""
DataFetcher tests — mocked NSE responses, NO live network calls.

Replaces the old test_get_spot(), which called the real NSE API directly
(CODE_REVIEW.md #18: "current tests hit live NSE/AI — flaky and slow").
Angel One isn't configured in a bare test environment (no ANGEL_* env vars),
so DataFetcher._try_angel_*() naturally return None and every call below
falls through to the NSE code path — we only need to mock `DataFetcher._get`
(the one low-level method that actually talks to curl_cffi/NSE) to control
exactly what NSE "returned" for each test, without touching real sessions,
cookies, or the network at all.

Several of these methods carry @async_cache — see conftest.py's
autouse fixture, which resets that cache before/after every test so one
test's mocked response can never leak into the next test that calls the
same method with the same arguments.

Requires: pip install -r requirements.txt -r requirements-dev.txt
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.services.data_fetcher import DataFetcher
from app.exceptions import MarketDataError


NSE_SPOT_RESPONSE = {
    "data": [{
        "lastPrice": 24650.5, "change": 120.3, "pChange": 0.49,
        "dayHigh": 24700.0, "dayLow": 24500.0, "open": 24550.0,
        "previousClose": 24530.2, "totalTradedVolume": 123456789,
    }],
    "marketStatus": {"marketStatus": "Open"},
}

NSE_OPTION_CHAIN_RESPONSE = {
    "records": {
        "expiryDates": ["28-Aug-2099", "04-Sep-2099"],
        "underlyingValue": 24650.5,
        "data": [
            {
                "strikePrice": 24600, "expiryDate": "28-Aug-2099",
                "CE": {"openInterest": 100000, "changeinOpenInterest": 5000,
                       "totalTradedVolume": 20000, "lastPrice": 120.5,
                       "impliedVolatility": 14.2, "bidprice": 120.0, "askPrice": 121.0},
                "PE": {"openInterest": 90000, "changeinOpenInterest": -2000,
                       "totalTradedVolume": 18000, "lastPrice": 95.0,
                       "impliedVolatility": 13.8, "bidprice": 94.5, "askPrice": 95.5},
            },
            {
                "strikePrice": 24700, "expiryDate": "28-Aug-2099",
                "CE": {"openInterest": 80000, "changeinOpenInterest": -1000,
                       "totalTradedVolume": 15000, "lastPrice": 80.0,
                       "impliedVolatility": 14.5, "bidprice": 79.5, "askPrice": 80.5},
                "PE": {"openInterest": 60000, "changeinOpenInterest": 3000,
                       "totalTradedVolume": 12000, "lastPrice": 130.0,
                       "impliedVolatility": 14.0, "bidprice": 129.5, "askPrice": 130.5},
            },
        ],
    },
    "filtered": {},
}

NSE_ALL_INDICES_WITH_VIX = {
    "data": [
        {"indexSymbol": "NIFTY 50", "last": 24650.5,
         "advance": {"advances": 32, "declines": 16, "unchanged": 2}},
        {"indexSymbol": "India VIX", "last": 13.45, "lastPrice": 13.45},
    ]
}


@pytest.mark.asyncio
async def test_get_spot_parses_nse_shape_correctly():
    fetcher = DataFetcher()
    with patch.object(DataFetcher, "_get", new=AsyncMock(return_value=NSE_SPOT_RESPONSE)):
        data = await fetcher.get_spot("NIFTY")
    assert data["price"] == 24650.5
    assert data["change"] == 120.3
    assert data["high"] == 24700.0
    assert data["low"] == 24500.0
    assert data["market_open"] is True
    assert data["market_status_source"] == "nse"


@pytest.mark.asyncio
async def test_get_spot_raises_market_data_error_when_nse_returns_empty():
    fetcher = DataFetcher()
    with patch.object(DataFetcher, "_get", new=AsyncMock(return_value={"data": []})):
        with pytest.raises(MarketDataError):
            await fetcher.get_spot("NIFTY")


@pytest.mark.asyncio
async def test_get_spot_rejects_unsupported_symbol():
    fetcher = DataFetcher()
    with pytest.raises(ValueError):
        await fetcher.get_spot("RANDOMSTOCK")


@pytest.mark.asyncio
async def test_get_option_chain_parses_records_block():
    """Regression test for CODE_REVIEW.md Round 1's critical parsing bug:
    NSE's real shape is {"records": {"data": [...], "expiryDates": [...]}},
    NOT a top-level "data" key. This locks that shape in."""
    fetcher = DataFetcher()
    with patch.object(DataFetcher, "_get", new=AsyncMock(return_value=NSE_OPTION_CHAIN_RESPONSE)):
        chain = await fetcher.get_option_chain("NIFTY")
    assert chain["expiry"] == "28-Aug-2099"
    assert chain["underlying_price"] == 24650.5
    assert len(chain["data"]) == 2
    assert chain["data"][0]["strikePrice"] == 24600


@pytest.mark.asyncio
async def test_get_option_chain_raises_when_records_missing():
    fetcher = DataFetcher()
    with patch.object(DataFetcher, "_get", new=AsyncMock(return_value={"filtered": {}})):
        with pytest.raises(MarketDataError):
            await fetcher.get_option_chain("NIFTY")


@pytest.mark.asyncio
async def test_get_volatility_finds_india_vix_by_partial_match():
    """Regression test for the 'INDIA VIX' exact-match bug fix — NSE's real
    symbol is 'India VIX' (mixed case), which the old exact-uppercase-match
    code missed entirely."""
    fetcher = DataFetcher()
    with patch.object(DataFetcher, "_get", new=AsyncMock(return_value=NSE_ALL_INDICES_WITH_VIX)):
        vix = await fetcher.get_volatility()
    assert vix == 13.45


@pytest.mark.asyncio
async def test_get_volatility_returns_zero_not_fake_value_when_unavailable():
    """Spec §42: never fabricate VIX when the real value can't be found."""
    fetcher = DataFetcher()
    with patch.object(DataFetcher, "_get", new=AsyncMock(return_value={"data": []})):
        vix = await fetcher.get_volatility()
    assert vix == 0.0


@pytest.mark.asyncio
async def test_get_market_breadth_reads_nifty50_advance_block():
    fetcher = DataFetcher()
    with patch.object(DataFetcher, "_get", new=AsyncMock(return_value=NSE_ALL_INDICES_WITH_VIX)):
        breadth = await fetcher.get_market_breadth()
    assert breadth["advances"] == 32
    assert breadth["declines"] == 16
    assert breadth["source"] == "nse_allIndices"


@pytest.mark.asyncio
async def test_get_fii_dii_reports_unavailable_on_error_not_fake_zero():
    """Spec §42: a blocked/unavailable source must say so explicitly
    (source='unavailable'), never silently return {fii: 0, dii: 0} as if
    that were a real reading."""
    fetcher = DataFetcher()
    with patch.object(DataFetcher, "_get", new=AsyncMock(side_effect=MarketDataError("403"))):
        result = await fetcher.get_fii_dii()
    assert result["source"] == "unavailable"
    assert result["fii"] is None
    assert result["dii"] is None
