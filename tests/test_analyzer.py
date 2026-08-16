"""
MarketAnalyzer tests — mocked DataFetcher + mocked DB/global-market calls,
NO live network calls.

Replaces the old test_full_market_overview(), which instantiated a real
DataFetcher and hit live NSE/AI/global-market endpoints directly
(CODE_REVIEW.md #18). MarketAnalyzer's constructor already accepts an
injected `fetcher`, which is exactly the seam this needs — no monkeypatching
of DataFetcher internals required, only of the DB-backed history_service
calls and the global_market_service singleton that get_full_market_overview
also reaches out to.

Requires: pip install -r requirements.txt -r requirements-dev.txt
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.services.market_analyzer import MarketAnalyzer


def _mock_fetcher(spot_price: float = 24650.0, prices=None, market_open: bool = True):
    """A fully-stubbed DataFetcher — every method MarketAnalyzer calls is
    an AsyncMock returning a minimal-but-shaped-correctly response, so
    get_full_market_overview can run its entire pipeline with zero network
    calls."""
    fetcher = AsyncMock()
    fetcher.get_spot.return_value = {
        "symbol": "NIFTY", "price": spot_price, "change": 50.0, "change_percent": 0.2,
        "high": spot_price + 100, "low": spot_price - 100, "open": spot_price - 20,
        "prev_close": spot_price - 50, "volume": 100000,
        "market_open": market_open, "market_status_source": "nse",
    }
    fetcher.get_option_chain.return_value = {
        "symbol": "NIFTY", "expiry": "28-Aug-2025", "all_expiries": ["28-Aug-2025"],
        "underlying_price": spot_price,
        "data": [
            {"strikePrice": spot_price - 50, "expiryDate": "28-Aug-2025",
             "CE": {"openInterest": 50000, "changeinOpenInterest": 1000, "totalTradedVolume": 5000,
                    "lastPrice": 130.0, "impliedVolatility": 14.0, "bidprice": 129.5, "askPrice": 130.5},
             "PE": {"openInterest": 80000, "changeinOpenInterest": 3000, "totalTradedVolume": 6000,
                    "lastPrice": 90.0, "impliedVolatility": 13.5, "bidprice": 89.5, "askPrice": 90.5}},
            {"strikePrice": spot_price + 50, "expiryDate": "28-Aug-2025",
             "CE": {"openInterest": 70000, "changeinOpenInterest": -500, "totalTradedVolume": 4000,
                    "lastPrice": 90.0, "impliedVolatility": 14.2, "bidprice": 89.5, "askPrice": 90.5},
             "PE": {"openInterest": 40000, "changeinOpenInterest": 500, "totalTradedVolume": 3000,
                    "lastPrice": 130.0, "impliedVolatility": 13.8, "bidprice": 129.5, "askPrice": 130.5}},
        ],
    }
    fetcher.get_volatility.return_value = 13.5
    fetcher.get_market_breadth.return_value = {"advances": 30, "declines": 18, "unchanged": 2, "source": "nse_allIndices"}
    fetcher.get_historical_prices.return_value = {
        "closes": prices if prices is not None else [],
        "volumes": [1000.0] * len(prices) if prices else [],
    }
    fetcher.get_intraday_ohlc.return_value = {"available": False}  # Angel One not configured
    fetcher.get_futures_premium.return_value = {"status": "unavailable", "premium": 0.0, "premium_pct": 0.0}
    fetcher.get_fii_dii.return_value = {"date": None, "fii": None, "dii": None, "source": "unavailable"}
    return fetcher


@pytest.fixture(autouse=True)
def _patch_db_and_global_calls():
    """These three are the pipeline's only non-fetcher external calls —
    stub them so nothing touches a real DB or the internet."""
    with patch("app.services.market_analyzer.save_option_chain_snapshot", new=AsyncMock(return_value=None)), \
         patch("app.services.market_analyzer.global_market_service.get_snapshot",
               new=AsyncMock(return_value={"instruments": {}, "global_change_pct": None, "gift_nifty_change_pct": None})), \
         patch("app.services.market_analyzer.history_service.get_oi_change_since",
               new=AsyncMock(return_value={"available": False, "reason": "test stub"})):
        yield


@pytest.mark.asyncio
async def test_full_overview_runs_end_to_end_with_insufficient_history():
    """No historical closes at all → must use the placeholder technicals
    path (spec §7: never fabricate indicators from data that isn't there)
    and still return a complete, well-shaped result rather than crashing."""
    analyzer = MarketAnalyzer(fetcher=_mock_fetcher(prices=[]))
    result = await analyzer.get_full_market_overview("NIFTY")

    assert result["technical_data_source"] == "placeholder"
    assert result["trend"] == "sideways"
    assert result["spot"]["price"] == 24650.0
    assert "decision" in result
    assert result["decision"]["market_bias"] in ("Bullish", "Bearish", "Sideways")
    assert "tamil_indicators" in result
    assert "reasons" in result["decision"]


@pytest.mark.asyncio
async def test_full_overview_uses_real_technicals_with_enough_history():
    """>=20 daily closes → must switch to the real historical_daily_close
    path instead of the placeholder."""
    # trend_detection() needs ma5 > ma20 * 1.005 (a deliberate 0.5% noise
    # band so flat/choppy markets don't get misread as trending — see
    # technical_indicators.py). i*5 only produces a ~0.15% ma5/ma20 gap over
    # 60 points, which correctly reads as "sideways" by that rule; it just
    # isn't steep enough to exercise the "bullish" branch this test is
    # actually meant to check. i*20 clears the threshold with headroom
    # (~0.6% gap) while still being a realistic daily-close uptrend.
    prices = [24000.0 + i * 20 for i in range(60)]  # steady uptrend, 60 points
    analyzer = MarketAnalyzer(fetcher=_mock_fetcher(prices=prices))
    result = await analyzer.get_full_market_overview("NIFTY")

    assert result["technical_data_source"] == "historical_daily_close"
    assert result["trend"] == "bullish"
    assert result["technicals"]["ema20"] > 0


@pytest.mark.asyncio
async def test_market_closed_flag_propagates_to_decision():
    """spot.market_open=False must reach market_data['market_open'] and,
    via run_decision_engine, force a no-trade read regardless of otherwise
    bullish-looking indicators (see test_decision_engine.py for the
    decision-engine half of this contract)."""
    analyzer = MarketAnalyzer(fetcher=_mock_fetcher(market_open=False))
    result = await analyzer.get_full_market_overview("NIFTY")

    assert result["market_open"] is False
    assert result["decision"]["market_bias"] == "Sideways"
    assert result["decision"]["preferred_side"] == "NONE"


@pytest.mark.asyncio
async def test_oi_summary_and_pcr_reflect_mocked_chain():
    analyzer = MarketAnalyzer(fetcher=_mock_fetcher())
    result = await analyzer.get_full_market_overview("NIFTY")

    # From the fixture: total PE OI = 80000+40000=120000, total CE OI = 50000+70000=120000
    assert result["pcr"] == 1.0
    assert "oi_summary" in result
    assert "support_resistance" in result


@pytest.mark.asyncio
async def test_option_chain_fetch_failure_degrades_gracefully():
    """spec §5/§35: one enrichment source failing (option chain blocked)
    must not take down the whole overview — spot/technicals should still
    come back."""
    fetcher = _mock_fetcher()
    fetcher.get_option_chain.side_effect = Exception("NSE blocked")
    analyzer = MarketAnalyzer(fetcher=fetcher)
    result = await analyzer.get_full_market_overview("NIFTY")

    assert result["spot"]["price"] == 24650.0
    assert result["option_chain"]["data"] == []
    assert result["pcr"] == 0.0


@pytest.mark.asyncio
async def test_expiry_param_is_forwarded_to_option_chain_fetch():
    """Analysis-page expiry selector: get_full_market_overview(symbol,
    expiry=...) must forward that expiry straight to
    fetcher.get_option_chain, so /api/analysis/ai and
    /api/strategy/recommend can return a suggestion for whichever expiry
    the user picked — not always the nearest one."""
    fetcher = _mock_fetcher()
    analyzer = MarketAnalyzer(fetcher=fetcher)

    await analyzer.get_full_market_overview("NIFTY", expiry="04-Sep-2025")
    fetcher.get_option_chain.assert_called_with("NIFTY", expiry="04-Sep-2025")

    # Omitting expiry must keep the previous nearest-expiry default (None),
    # not silently reuse whatever the last call happened to pass.
    await analyzer.get_full_market_overview("BANKNIFTY")
    fetcher.get_option_chain.assert_called_with("BANKNIFTY", expiry=None)


@pytest.mark.asyncio
async def test_different_expiries_do_not_share_a_cache_entry():
    """get_full_market_overview is @async_cache'd by its args — picking a
    different expiry for the same symbol must be a cache miss (fresh
    fetch), not silently return the previously-cached expiry's data."""
    fetcher = _mock_fetcher()
    analyzer = MarketAnalyzer(fetcher=fetcher)

    await analyzer.get_full_market_overview("NIFTY", expiry="28-Aug-2025")
    await analyzer.get_full_market_overview("NIFTY", expiry="04-Sep-2025")

    assert fetcher.get_spot.call_count == 2  # one real fetch per distinct expiry, no cache hit

    # Same symbol + same expiry again, within the TTL window → cache hit, no new fetch.
    await analyzer.get_full_market_overview("NIFTY", expiry="28-Aug-2025")
    assert fetcher.get_spot.call_count == 2
