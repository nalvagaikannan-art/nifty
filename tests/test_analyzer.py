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
from app.utils.cache import _cache


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
    fetcher._try_angel_live_feed_spot.return_value = None
    fetcher._get_spot_uncached.return_value = fetcher.get_spot.return_value
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
    """Keep analyzer unit tests isolated from real DB/cache/network state."""
    _cache.clear()
    with (
        patch("app.services.market_analyzer.is_market_hours_ist", return_value=True),
        patch("app.utils.cache._redis_client", None),
        patch(
            "app.services.market_analyzer.save_option_chain_snapshot",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.market_analyzer.history_service.save_market_snapshot",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.market_analyzer.global_market_service.get_snapshot",
            new=AsyncMock(
                return_value={
                    "instruments": {},
                    "global_change_pct": None,
                    "gift_nifty_change_pct": None,
                }
            ),
        ),
        patch(
            "app.services.market_analyzer.history_service.get_oi_change_since",
            new=AsyncMock(
                return_value={
                    "available": False,
                    "reason": "test stub",
                }
            ),
        ),
        patch(
            "app.services.market_analyzer.history_service.get_latest_intraday_ohlc_before",
            new=AsyncMock(return_value=[]),
        ),
    ):
        yield
    _cache.clear()


@pytest.mark.asyncio
async def test_full_overview_runs_end_to_end_with_insufficient_history():
    """No historical closes at all → must use the placeholder technicals
    path (spec §7: never fabricate indicators from data that isn't there)
    and still return a complete, well-shaped result rather than crashing."""
    analyzer = MarketAnalyzer(fetcher=_mock_fetcher(prices=[]))
    with patch("app.services.market_analyzer.is_market_hours_ist", return_value=True):
        result = await analyzer.get_full_market_overview("NIFTY")

    assert result["technical_data_source"] == "placeholder"
    assert result["trend"] == "sideways"
    assert result["spot"]["price"] == 24650.0
    assert "decision" in result
    assert result["decision"]["market_bias"] in ("Bullish", "Bearish", "Sideways")
    assert "tamil_indicators" in result
    assert "reasons" in result["decision"]
    quality = result["option_chain_quality"]
    assert quality["available"] is True
    assert quality["status"] == "limited"  # fixture has only two strikes
    assert quality["oi_coverage_pct"] == 100.0
    assert quality["volume_coverage_pct"] == 100.0
    assert result["pcr_quality"]["chain_quality"] == quality


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
    fetcher.get_option_chain.assert_called_with("NIFTY", "04-Sep-2025")

    # Omitting expiry must keep the previous nearest-expiry default (None),
    # not silently reuse whatever the last call happened to pass.
    await analyzer.get_full_market_overview("BANKNIFTY")
    fetcher.get_option_chain.assert_called_with("BANKNIFTY", None)


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

@pytest.mark.asyncio
async def test_safe_multi_timeframe_excludes_current_live_5m_bucket():
    """Live REST candles may contain the currently-forming 5-minute bar.

    The live MTF frame must use only completed candles. The exact current
    5-minute bucket must not reach 5m indicators or downstream MTF output.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    fixed_now = datetime(2026, 10, 8, 11, 7, 0, tzinfo=ist)

    # 09:15 ... 11:05 inclusive.
    timestamps = []
    cur = datetime(2026, 10, 8, 9, 15, tzinfo=ist)
    end = datetime(2026, 10, 8, 11, 5, tzinfo=ist)

    while cur <= end:
        timestamps.append(cur.isoformat())
        cur += timedelta(minutes=5)

    count = len(timestamps)
    fetcher = _mock_fetcher()
    fetcher.get_intraday_ohlc.return_value = {
        "available": True,
        "data_source": "angel_one_intraday",
        "opens": [100.0 + i for i in range(count)],
        "highs": [101.0 + i for i in range(count)],
        "lows": [99.0 + i for i in range(count)],
        "closes": [100.5 + i for i in range(count)],
        "volumes": [1000] * count,
        "timestamps": timestamps,
    }

    analyzer = MarketAnalyzer(fetcher=fetcher)

    with (
        patch("app.services.market_analyzer.current_ist", return_value=fixed_now),
        patch(
            "app.services.market_analyzer.angel_live_feed.get_completed_5m_candles",
            return_value=[],
        ),
        patch(
            "app.services.market_analyzer.history_service.save_intraday_ohlc_snapshot",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await analyzer._safe_multi_timeframe("NIFTY")

    five = result["5min"]

    assert five["timestamps"]
    assert five["timestamps"][-1] == "2026-10-08T11:00:00+05:30"
    assert "2026-10-08T11:05:00+05:30" not in five["timestamps"]

@pytest.mark.asyncio
async def test_safe_multi_timeframe_ws_zero_volume_preserves_rest_proxy_volume():
    """Zero-volume WS candles must not erase aligned REST/proxy volume."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    fixed_now = datetime(2026, 10, 8, 11, 7, 0, tzinfo=ist)

    timestamps = []
    cur = datetime(2026, 10, 8, 9, 15, tzinfo=ist)
    end = datetime(2026, 10, 8, 11, 5, tzinfo=ist)
    while cur <= end:
        timestamps.append(cur.isoformat())
        cur += timedelta(minutes=5)

    count = len(timestamps)
    proxy_volumes = [2000 + i for i in range(count)]

    fetcher = _mock_fetcher()
    fetcher.get_intraday_ohlc.return_value = {
        "available": True,
        "data_source": "angel_one_intraday",
        "opens": [100.0 + i for i in range(count)],
        "highs": [101.0 + i for i in range(count)],
        "lows": [99.0 + i for i in range(count)],
        "closes": [100.5 + i for i in range(count)],
        "volumes": proxy_volumes,
        "timestamps": timestamps,
    }

    # Simulate the index WS feed reporting zero volume at REST timestamps.
    ws_candles = []
    for i, text in enumerate(timestamps):
        ts = datetime.fromisoformat(text)
        ws_candles.append({
            "timestamp": ts.timestamp(),
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 0.0,
        })

    analyzer = MarketAnalyzer(fetcher=fetcher)
    with (
        patch("app.services.market_analyzer.current_ist", return_value=fixed_now),
        patch(
            "app.services.market_analyzer.angel_live_feed.get_completed_5m_candles",
            return_value=ws_candles,
        ),
        patch(
            "app.services.market_analyzer.history_service.save_intraday_ohlc_snapshot",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await analyzer._safe_multi_timeframe("NIFTY")

    five = result["5min"]
    assert five["timestamps"][-1] == "2026-10-08T11:00:00+05:30"
    assert five["volumes"] == proxy_volumes[:-1]
    assert all(v > 0 for v in five["volumes"])




@pytest.mark.asyncio
async def test_safe_multi_timeframe_ws_merge_keeps_ws_ohlc_open_aligned():
    """Persist WS open with WS high/low/close when REST open disagrees."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    fixed_now = datetime(2026, 10, 8, 11, 7, 0, tzinfo=ist)
    timestamps = []
    cur = datetime(2026, 10, 8, 9, 15, tzinfo=ist)
    end = datetime(2026, 10, 8, 11, 5, tzinfo=ist)
    while cur <= end:
        timestamps.append(cur.isoformat())
        cur += timedelta(minutes=5)

    count = len(timestamps)
    fetcher = _mock_fetcher()
    fetcher.get_intraday_ohlc.return_value = {
        "available": True,
        "data_source": "angel_one_intraday",
        "opens": [102.0 + i for i in range(count)],
        "highs": [101.0 + i for i in range(count)],
        "lows": [99.0 + i for i in range(count)],
        "closes": [100.5 + i for i in range(count)],
        "volumes": [2000 + i for i in range(count)],
        "timestamps": timestamps,
    }

    ws_candles = []
    for i, text in enumerate(timestamps):
        ts = datetime.fromisoformat(text)
        ws_candles.append({
            "timestamp": ts.timestamp(),
            "open": 100.25 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "volume": 0.0,
        })

    analyzer = MarketAnalyzer(fetcher=fetcher)
    save_snapshot = AsyncMock(return_value=None)
    with (
        patch("app.services.market_analyzer.current_ist", return_value=fixed_now),
        patch(
            "app.services.market_analyzer.angel_live_feed.get_completed_5m_candles",
            return_value=ws_candles,
        ),
        patch(
            "app.services.market_analyzer.history_service.save_intraday_ohlc_snapshot",
            new=save_snapshot,
        ),
    ):
        result = await analyzer._safe_multi_timeframe("NIFTY")

    assert result["5min"]["timestamps"][-1] == "2026-10-08T11:00:00+05:30"
    assert save_snapshot.await_args is not None

    persisted_rows = save_snapshot.await_args.args[1]
    open_map = save_snapshot.await_args.kwargs["open_by_timestamp"]
    first_ts = datetime.fromisoformat(timestamps[0])
    first_row = next(row for row in persisted_rows if row[0] == first_ts)

    assert open_map[first_ts] == pytest.approx(100.25)
    assert first_row[1] == pytest.approx(101.0)
    assert first_row[2] == pytest.approx(99.0)
    assert first_row[3] == pytest.approx(100.5)

    for row in persisted_rows:
        op = open_map[row[0]]
        hi, lo, close = row[1], row[2], row[3]
        assert hi >= max(op, close)
        assert lo <= min(op, close)
        assert lo <= hi

@pytest.mark.asyncio
async def test_live_refresh_uses_uncached_fallback_when_ws_tick_is_unavailable():
    fetcher = _mock_fetcher()
    analyzer = MarketAnalyzer(fetcher=fetcher)
    cached = await analyzer._get_full_market_overview_cached(
        "NIFTY", cache_bust="spot-fallback-regression"
    )

    fallback_spot = dict(fetcher.get_spot.return_value)
    fallback_spot.update({
        "price": 24725.0,
        "change": 125.0,
        "change_percent": 0.51,
        "data_source": "zerodha",
        "timestamp": "2026-10-15T11:00:00+05:30",
    })
    fetcher._try_angel_live_feed_spot.return_value = None
    fetcher._get_spot_uncached.return_value = fallback_spot

    result = await analyzer._refresh_live_calculations(cached, "NIFTY")

    assert result["spot"]["price"] == 24725.0
    assert result["spot"]["data_source"] == "zerodha"
    fetcher._get_spot_uncached.assert_awaited_once_with("NIFTY")


@pytest.mark.asyncio
async def test_live_refresh_does_not_fake_new_timestamp_when_all_spot_sources_fail():
    fetcher = _mock_fetcher()
    analyzer = MarketAnalyzer(fetcher=fetcher)
    cached = await analyzer._get_full_market_overview_cached(
        "NIFTY", cache_bust="spot-unavailable-regression"
    )
    old_timestamp = "2026-10-15T10:30:00+05:30"
    cached["timestamp"] = old_timestamp

    fetcher._try_angel_live_feed_spot.return_value = None
    fetcher._get_spot_uncached.return_value = None

    result = await analyzer._refresh_live_calculations(cached, "NIFTY")

    assert result is cached
    assert result["timestamp"] == old_timestamp
    fetcher._get_spot_uncached.assert_awaited_once_with("NIFTY")


@pytest.mark.asyncio
async def test_live_refresh_preserves_cached_ohlc_when_ws_fields_are_zero():
    fetcher = _mock_fetcher()
    analyzer = MarketAnalyzer(fetcher=fetcher)
    cached = await analyzer._get_full_market_overview_cached(
        "NIFTY", cache_bust="spot-zero-ohlc-regression"
    )
    previous_spot = dict(cached["spot"])

    live_spot = dict(previous_spot)
    live_spot.update({
        "price": 24700.0,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "prev_close": 0.0,
        "change": 0.0,
        "change_percent": 0.0,
        "volume": 0,
        "data_source": "angel_one_websocket",
        "timestamp": "2026-10-15T11:00:00+05:30",
        "exchange_timestamp": 1792033200000,
        "tick_age_sec": 0.5,
    })
    fetcher._try_angel_live_feed_spot.return_value = live_spot

    result = await analyzer._refresh_live_calculations(cached, "NIFTY")

    assert result["spot"]["price"] == 24700.0
    for field in ("open", "high", "low"):
        assert result["spot"][field] == previous_spot[field]
    assert result["spot"]["prev_close"] == previous_spot["prev_close"]
    assert result["option_chain_quality"]["status"] == "limited"
    assert (
        result["pcr_quality"]["chain_quality"]
        == result["option_chain_quality"]
    )

def _closed_market_test_spot():
    return {
        "symbol": "NIFTY", "price": 24650.0,
        "change": 50.0, "change_percent": 0.2,
        "high": 24750.0, "low": 24550.0, "open": 24630.0,
        "prev_close": 24600.0, "volume": 100000,
        "timestamp": "2026-10-21T09:59:00+00:00",
        "market_open": False,
    }


def _closed_market_test_chain(expiry="27OCT2026"):
    chain = dict(_mock_fetcher().get_option_chain.return_value)
    chain.update({
        "symbol": "NIFTY", "expiry": expiry,
        "all_expiries": [expiry, "17NOV2026"],
        "data_source": "history_db_closed_fallback",
        "snapshot_timestamp": "2026-10-21T09:59:28",
        "market_open": False, "stale": True,
    })
    return chain


@pytest.mark.asyncio
async def test_closed_market_default_expiry_uses_persisted_nearest_chain():
    fetcher = _mock_fetcher(market_open=False)
    analyzer = MarketAnalyzer(fetcher=fetcher)
    nearest_lookup = AsyncMock(return_value=_closed_market_test_chain())

    with (
        patch("app.services.market_analyzer.is_market_hours_ist", return_value=False),
        patch(
            "app.services.market_analyzer.history_service.get_latest_market_snapshot_before",
            new=AsyncMock(return_value=_closed_market_test_spot()),
        ),
        patch(
            "app.services.market_analyzer.history_service.get_latest_option_chain_snapshot_for_nearest_expiry_before",
            new=nearest_lookup,
        ),
    ):
        result = await analyzer.get_full_market_overview(
            "NIFTY", cache_bust="closed-default-expiry-regression"
        )

    nearest_lookup.assert_awaited_once()
    assert nearest_lookup.await_args.args[0] == "NIFTY"
    assert result["option_chain"]["expiry"] == "27OCT2026"
    assert result["option_chain"]["all_expiries"] == [
        "27OCT2026", "17NOV2026"
    ]
    assert result["market_open"] is False
    assert result["decision"]["preferred_side"] == "NONE"
    assert result["option_chain_quality"]["status"] == "limited"
    assert (
        result["pcr_quality"]["chain_quality"]
        == result["option_chain_quality"]
    )
    fetcher.get_option_chain.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_market_missing_chain_still_waits_without_live_fetch():
    fetcher = _mock_fetcher(market_open=False)
    analyzer = MarketAnalyzer(fetcher=fetcher)
    nearest_lookup = AsyncMock(return_value=None)

    with (
        patch("app.services.market_analyzer.is_market_hours_ist", return_value=False),
        patch(
            "app.services.market_analyzer.history_service.get_latest_market_snapshot_before",
            new=AsyncMock(return_value=_closed_market_test_spot()),
        ),
        patch(
            "app.services.market_analyzer.history_service.get_latest_option_chain_snapshot_for_nearest_expiry_before",
            new=nearest_lookup,
        ),
    ):
        result = await analyzer.get_full_market_overview(
            "NIFTY", cache_bust="closed-no-chain-regression"
        )

    nearest_lookup.assert_awaited_once()
    assert result["option_chain"]["data"] == []
    assert result["decision"]["preferred_side"] == "NONE"
    fetcher.get_option_chain.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_market_explicit_expiry_keeps_exact_snapshot_and_full_catalogue():
    fetcher = _mock_fetcher(market_open=False)
    analyzer = MarketAnalyzer(fetcher=fetcher)

    exact_chain = _closed_market_test_chain("17NOV2026")
    exact_lookup = AsyncMock(return_value=exact_chain)

    expiry_catalog = _closed_market_test_chain("27OCT2026")
    full_expiry_list = [
        "27OCT2026",
        "17NOV2026",
        "30MAR2027",
        "28SEP2027",
        "26DEC2028",
    ]
    expiry_catalog["all_expiries"] = full_expiry_list
    catalogue_lookup = AsyncMock(return_value=expiry_catalog)

    with (
        patch("app.services.market_analyzer.is_market_hours_ist", return_value=False),
        patch(
            "app.services.market_analyzer.history_service.get_latest_market_snapshot_before",
            new=AsyncMock(return_value=_closed_market_test_spot()),
        ),
        patch(
            "app.services.market_analyzer.history_service.get_latest_option_chain_snapshot_before",
            new=exact_lookup,
        ),
        patch(
            "app.services.market_analyzer.history_service.get_latest_option_chain_snapshot_for_nearest_expiry_before",
            new=catalogue_lookup,
        ),
    ):
        result = await analyzer.get_full_market_overview(
            "NIFTY",
            expiry="17NOV2026",
            cache_bust="closed-explicit-expiry-full-catalogue-regression",
        )

    exact_lookup.assert_awaited_once()
    assert exact_lookup.await_args.args[:2] == ("NIFTY", "17NOV2026")
    catalogue_lookup.assert_awaited_once()

    # Preserve the selected expiry, without shrinking the dropdown catalogue.
    assert result["option_chain"]["expiry"] == "17NOV2026"
    assert result["option_chain"]["all_expiries"] == full_expiry_list
    assert result["option_chain"]["data"]
    assert result["decision"]["preferred_side"] == "NONE"
    fetcher.get_option_chain.assert_not_awaited()

@pytest.mark.asyncio
async def test_missing_spot_market_open_fails_closed_in_overview(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_analyzer.is_market_hours_ist",
        lambda *_args: True,
    )

    fetcher = _mock_fetcher()
    fetcher.get_spot.return_value.pop("market_open", None)
    analyzer = MarketAnalyzer(fetcher=fetcher)

    result = await analyzer.get_full_market_overview(
        "NIFTY",
        cache_bust="market-open-missing-status-regression",
    )

    assert result["market_open"] is False
    assert result["decision"]["market_open"] is False
    assert result["decision"]["hard_gated"] is True
    assert result["decision"]["preferred_side"] == "NONE"



@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])
async def test_closed_market_missing_spot_raises_market_data_error_without_live_fetch(symbol):
    """Missing closed-session history is a typed unavailable condition, not
    an unhandled RuntimeError and never a reason to synthesize a spot/decision."""
    from app.exceptions import MarketDataError

    fetcher = _mock_fetcher(market_open=False)
    analyzer = MarketAnalyzer(fetcher=fetcher)
    missing_spot = AsyncMock(return_value=None)

    with (
        patch("app.services.market_analyzer.is_market_hours_ist", return_value=False),
        patch(
            "app.services.market_analyzer.history_service.get_latest_market_snapshot_before",
            new=missing_spot,
        ),
    ):
        with pytest.raises(MarketDataError, match=f"closed-market fallback: {symbol}"):
            await analyzer.get_full_market_overview(
                symbol,
                cache_bust=f"closed-missing-spot-{symbol.lower()}-regression",
            )

    missing_spot.assert_awaited_once()
    assert missing_spot.await_args.args[0] == symbol
    fetcher.get_spot.assert_not_awaited()
    fetcher.get_option_chain.assert_not_awaited()
