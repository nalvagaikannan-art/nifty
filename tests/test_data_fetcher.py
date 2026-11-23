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
import time
import pytest
from unittest.mock import AsyncMock, patch

from app.services.data_fetcher import DataFetcher
from app.exceptions import MarketDataError

# Capture original methods before the autouse fixture disables external brokers.
_ORIGINAL_TRY_ANGEL_OPTION_CHAIN = DataFetcher._try_angel_option_chain
_ORIGINAL_TRY_ZERODHA_OPTION_CHAIN = DataFetcher._try_zerodha_option_chain


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

@pytest.fixture(autouse=True)
def disable_external_brokers(monkeypatch):
    """DataFetcher tests must never use live broker data."""
    async def no_angel(*args, **kwargs):
        return None

    async def no_zerodha(*args, **kwargs):
        return None

    from app.services.angel_one import angel_session
    monkeypatch.setattr(angel_session, "get_india_vix", no_angel)

    monkeypatch.setattr(DataFetcher, "_try_angel_spot", no_angel)
    monkeypatch.setattr(DataFetcher, "_try_angel_option_chain", no_angel)
    monkeypatch.setattr(DataFetcher, "_try_zerodha_spot", no_zerodha)
    monkeypatch.setattr(DataFetcher, "_try_zerodha_option_chain", no_zerodha)


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
async def test_get_volatility_info_exposes_source_and_provider_time(monkeypatch):
    fetcher = DataFetcher()
    payload = {
        "data": [{
            "indexSymbol": "India VIX",
            "last": 13.45,
            "lastUpdateTime": "14-Oct-2026 15:29:00",
        }]
    }
    monkeypatch.setattr(
        fetcher, "_get_all_indices", AsyncMock(return_value=payload)
    )

    info = await fetcher.get_volatility_info()

    assert info["value"] == 13.45
    assert info["available"] is True
    assert info["source"] == "nse_allIndices"
    assert info["fetched_at"]
    assert info["observed_at"] == "14-Oct-2026 15:29:00"
    assert info["reason"] is None


@pytest.mark.asyncio
async def test_get_volatility_info_marks_missing_value_unavailable(monkeypatch):
    fetcher = DataFetcher()
    monkeypatch.setattr(
        fetcher, "_get_all_indices", AsyncMock(return_value={"data": []})
    )

    info = await fetcher.get_volatility_info()

    assert info["value"] == 0.0
    assert info["available"] is False
    assert info["source"] == "unavailable"
    assert info["fetched_at"]
    assert info["observed_at"] is None
    assert info["reason"]


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



def test_futures_volume_cache_is_scoped_by_symbol_and_interval(monkeypatch):
    import time

    fetcher = DataFetcher()
    timestamp = "2026-10-09T09:15:00+05:30"
    epoch = fetcher._candle_timestamp_key(timestamp)
    assert epoch is not None

    now = time.time()
    keys = [
        ("NIFTY", "FIVE_MINUTE"),
        ("NIFTY", "15_MINUTE"),
        ("BANKNIFTY", "FIVE_MINUTE"),
    ]
    monkeypatch.setattr(
        DataFetcher,
        "_FUTURES_VOLUME_CACHE",
        {
            keys[0]: {epoch: 111},
            keys[1]: {epoch: 222},
            keys[2]: {epoch: 333},
        },
    )
    monkeypatch.setattr(
        DataFetcher,
        "_FUTURES_VOLUME_CACHE_TS",
        {key: now for key in keys},
    )

    # Same timestamp, but each index/timeframe must receive its own value.
    assert fetcher._get_cached_futures_volumes(
        "NIFTY", [timestamp], "FIVE_MINUTE"
    ) == [111]
    assert fetcher._get_cached_futures_volumes(
        "NIFTY", [timestamp], "15_MINUTE"
    ) == [222]
    assert fetcher._get_cached_futures_volumes(
        "BANKNIFTY", [timestamp], "FIVE_MINUTE"
    ) == [333]
    assert fetcher._get_cached_futures_volumes(
        "NIFTY", [timestamp], "ONE_HOUR"
    ) is None


@pytest.mark.asyncio
async def test_futures_volume_refresh_tasks_are_scoped_by_interval(monkeypatch):
    import asyncio

    task_map = {}
    last_scheduled_map = {}
    monkeypatch.setattr(DataFetcher, "_FUTURES_VOLUME_TASKS", task_map)
    monkeypatch.setattr(
        DataFetcher, "_FUTURES_VOLUME_LAST_SCHEDULED_TS",
        last_scheduled_map,
    )

    async def blocked_refresh(self, symbol, interval, from_str, to_str):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        DataFetcher, "_refresh_futures_volume_proxy", blocked_refresh
    )

    fetcher = DataFetcher()
    fetcher._schedule_futures_volume_refresh(
        "NIFTY", "FIVE_MINUTE", "2026-10-09 09:15", "2026-10-09 10:00"
    )
    fetcher._schedule_futures_volume_refresh(
        "NIFTY", "15_MINUTE", "2026-10-09 09:15", "2026-10-09 10:00"
    )

    expected = {
        ("NIFTY", "FIVE_MINUTE"),
        ("NIFTY", "15_MINUTE"),
    }
    try:
        assert set(task_map) == expected
        assert task_map[("NIFTY", "FIVE_MINUTE")] is not task_map[
            ("NIFTY", "15_MINUTE")
        ]
        await asyncio.sleep(0)
        assert all(not task.done() for task in task_map.values())
    finally:
        pending = list(task_map.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def _coverage_timestamps(day, count):
    from datetime import datetime, timedelta

    start = datetime.fromisoformat(f"{day}T09:15:00+05:30")
    return [
        (start + timedelta(minutes=5 * i)).isoformat()
        for i in range(count)
    ]


def test_futures_volume_cache_rejects_latest_session_below_90_percent(monkeypatch):
    import time

    fetcher = DataFetcher()
    normalize = DataFetcher._candle_timestamp_key
    timestamps = _coverage_timestamps("2026-10-09", 74)
    cache_key = ("FINNIFTY", "FIVE_MINUTE")
    cached = {
        normalize(ts): 100 + i
        for i, ts in enumerate(timestamps[:44])
    }

    monkeypatch.setattr(
        DataFetcher, "_FUTURES_VOLUME_CACHE", {cache_key: cached}
    )
    monkeypatch.setattr(
        DataFetcher, "_FUTURES_VOLUME_CACHE_TS", {cache_key: time.time()}
    )

    assert fetcher._get_cached_futures_volumes(
        "FINNIFTY", timestamps, "FIVE_MINUTE"
    ) is None


def test_futures_volume_cache_preserves_observed_zero_volume(monkeypatch):
    import time

    fetcher = DataFetcher()
    normalize = DataFetcher._candle_timestamp_key
    timestamps = _coverage_timestamps("2026-10-09", 10)
    cached = {
        normalize(ts): 100 + i for i, ts in enumerate(timestamps)
    }
    cached[normalize(timestamps[3])] = 0
    cache_key = ("NIFTY", "FIVE_MINUTE")

    monkeypatch.setattr(
        DataFetcher, "_FUTURES_VOLUME_CACHE", {cache_key: cached}
    )
    monkeypatch.setattr(
        DataFetcher, "_FUTURES_VOLUME_CACHE_TS", {cache_key: time.time()}
    )

    result = fetcher._get_cached_futures_volumes(
        "NIFTY", timestamps, "FIVE_MINUTE"
    )
    assert result == [cached[normalize(ts)] for ts in timestamps]
    assert result[3] == 0


def test_futures_volume_coverage_uses_latest_session_not_history(monkeypatch):
    import time

    fetcher = DataFetcher()
    normalize = DataFetcher._candle_timestamp_key
    older = _coverage_timestamps("2026-10-08", 6)
    latest = _coverage_timestamps("2026-10-09", 6)
    timestamps = older + latest
    cache_key = ("NIFTY", "FIVE_MINUTE")
    cached = {
        normalize(ts): i + 1 for i, ts in enumerate(latest)
    }

    monkeypatch.setattr(
        DataFetcher, "_FUTURES_VOLUME_CACHE", {cache_key: cached}
    )
    monkeypatch.setattr(
        DataFetcher, "_FUTURES_VOLUME_CACHE_TS", {cache_key: time.time()}
    )

    result = fetcher._get_cached_futures_volumes(
        "NIFTY", timestamps, "FIVE_MINUTE"
    )
    assert result is not None
    assert result[:6] == [0] * 6
    assert result[6:] == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_futures_volume_refresh_cooldown_survives_task_completion(monkeypatch):
    import time

    tasks = {}
    scheduled = {}
    calls = []

    monkeypatch.setattr(DataFetcher, "_FUTURES_VOLUME_TASKS", tasks)
    monkeypatch.setattr(
        DataFetcher, "_FUTURES_VOLUME_LAST_SCHEDULED_TS", scheduled
    )

    async def fake_refresh(self, symbol, interval, from_str, to_str):
        calls.append((symbol, interval))

    monkeypatch.setattr(
        DataFetcher, "_refresh_futures_volume_proxy", fake_refresh
    )

    fetcher = DataFetcher()
    key = ("FINNIFTY", "FIVE_MINUTE")
    args = ("FINNIFTY", "FIVE_MINUTE", "2026-10-09 09:15", "2026-10-09 15:30")

    assert fetcher._schedule_futures_volume_refresh(*args) is True
    first_task = tasks[key]
    await first_task
    assert calls == [("FINNIFTY", "FIVE_MINUTE")]

    # A completed task must not trigger another request during cooldown.
    assert fetcher._schedule_futures_volume_refresh(*args) is False
    assert tasks[key] is first_task
    assert calls == [("FINNIFTY", "FIVE_MINUTE")]

    # The same symbol/interval is eligible again once cooldown has elapsed.
    scheduled[key] = (
        time.time() - DataFetcher._FUTURES_VOLUME_REFRESH_COOLDOWN - 1
    )
    assert fetcher._schedule_futures_volume_refresh(*args) is True
    second_task = tasks[key]
    assert second_task is not first_task
    await second_task
    assert calls == [
        ("FINNIFTY", "FIVE_MINUTE"),
        ("FINNIFTY", "FIVE_MINUTE"),
    ]

def test_option_snapshot_persistence_shared_cooldown():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.services import data_fetcher as fetcher_module
    from app.services import history_service
    from app.services.option_analyzer import OptionAnalyzer
    from app.services.option_snapshot_state import snapshot_persistence_enabled
    import app.utils.helpers as helpers

    class FakeOptionFrame:
        empty = False

        def __len__(self):
            return 21

    async def scenario():
        old_last = fetcher_module._OPTION_SNAPSHOT_PERSIST_LAST_SAVED
        old_lock = fetcher_module._OPTION_SNAPSHOT_PERSIST_LOCK
        fetcher_module._OPTION_SNAPSHOT_PERSIST_LAST_SAVED = {}
        fetcher_module._OPTION_SNAPSHOT_PERSIST_LOCK = asyncio.Lock()
        token = snapshot_persistence_enabled.set(True)
        calls = []

        async def fake_save(symbol, expiry, option_df):
            calls.append((symbol, expiry))
            return True

        try:
            with patch.object(helpers, "is_market_hours_ist", return_value=True), \
                 patch.object(OptionAnalyzer, "process_option_chain",
                              return_value=FakeOptionFrame()), \
                 patch.object(history_service, "save_option_chain_snapshot",
                              new=fake_save):
                chain = {
                    "symbol": "NIFTY",
                    "expiry": "27OCT2026",
                    "data": [{"strikePrice": 25000}],
                }

                first = await fetcher_module._persist_option_chain_snapshot_if_due(
                    chain, "NIFTY", now_mono=1000.0
                )
                too_soon = await fetcher_module._persist_option_chain_snapshot_if_due(
                    chain, "NIFTY", now_mono=1059.9
                )
                due_again = await fetcher_module._persist_option_chain_snapshot_if_due(
                    chain, "NIFTY", now_mono=1060.0
                )

                assert first is True
                assert too_soon is False
                assert due_again is True
                assert calls == [
                    ("NIFTY", "27OCT2026"),
                    ("NIFTY", "27OCT2026"),
                ]
        finally:
            snapshot_persistence_enabled.reset(token)
            fetcher_module._OPTION_SNAPSHOT_PERSIST_LAST_SAVED = old_last
            fetcher_module._OPTION_SNAPSHOT_PERSIST_LOCK = old_lock

    asyncio.run(scenario())

def test_try_angel_option_chain_prefers_provider_expiry(monkeypatch):
    import asyncio
    from unittest.mock import patch
    import importlib
    angel_one_module = importlib.import_module("app.services.angel_one")
    from app.services.data_fetcher import DataFetcher
    monkeypatch.setattr(
        DataFetcher, "_try_angel_option_chain",
        _ORIGINAL_TRY_ANGEL_OPTION_CHAIN,
    )

    chain = {
        "symbol": "FINNIFTY",
        "expiry": "27OCT2026",
        "underlying_price": 25000.0,
        "data": [{"strikePrice": 25000, "CE": {}, "PE": {}}],
    }

    class FakeSession:
        is_configured = True

        async def get_option_chain(self, *args, **kwargs):
            return chain

    async def scenario():
        with patch.object(angel_one_module, "angel_session", FakeSession()):
            result = await DataFetcher()._try_angel_option_chain(
                "FINNIFTY", "27-Oct-2026",
                spot_price=25000.0, strikes_each_side=10
            )
        assert result is not None
        assert result["expiry"] == "27OCT2026"

    asyncio.run(scenario())


def test_try_zerodha_option_chain_prefers_provider_expiry(monkeypatch):
    import asyncio
    from unittest.mock import patch
    import importlib
    zerodha_module = importlib.import_module("app.services.zerodha")
    from app.services.data_fetcher import DataFetcher
    monkeypatch.setattr(
        DataFetcher, "_try_zerodha_option_chain",
        _ORIGINAL_TRY_ZERODHA_OPTION_CHAIN,
    )

    chain = {
        "symbol": "FINNIFTY",
        "expiry": "27OCT2026",
        "all_expiries": ["27OCT2026"],
        "underlying_price": 25000.0,
        "data": [{"strikePrice": 25000, "CE": {}, "PE": {}}],
    }

    class FakeSession:
        is_configured = True

        async def get_option_chain(self, *args, **kwargs):
            return chain

    async def scenario():
        with patch.object(zerodha_module, "kite_session", FakeSession()):
            result = await DataFetcher()._try_zerodha_option_chain(
                "FINNIFTY", "27-Oct-2026",
                strikes_each_side=10
            )
        assert result is not None
        assert result["expiry"] == "27OCT2026"

    asyncio.run(scenario())

def test_option_snapshot_persistence_retries_after_failed_save():
    import asyncio
    from unittest.mock import patch

    from app.services import data_fetcher as fetcher_module
    from app.services import history_service
    from app.services.option_analyzer import OptionAnalyzer
    from app.services.option_snapshot_state import snapshot_persistence_enabled
    import app.utils.helpers as helpers

    class FakeOptionFrame:
        empty = False

        def __len__(self):
            return 21

    async def scenario():
        old_last = fetcher_module._OPTION_SNAPSHOT_PERSIST_LAST_SAVED
        old_lock = fetcher_module._OPTION_SNAPSHOT_PERSIST_LOCK
        fetcher_module._OPTION_SNAPSHOT_PERSIST_LAST_SAVED = {}
        fetcher_module._OPTION_SNAPSHOT_PERSIST_LOCK = asyncio.Lock()
        token = snapshot_persistence_enabled.set(True)
        outcomes = iter([False, True])
        calls = []

        async def fake_save(symbol, expiry, option_df):
            calls.append((symbol, expiry))
            return next(outcomes)

        try:
            with patch.object(helpers, "is_market_hours_ist", return_value=True), patch.object(
                OptionAnalyzer, "process_option_chain",
                return_value=FakeOptionFrame()
            ), patch.object(
                history_service, "save_option_chain_snapshot",
                new=fake_save
            ):
                chain = {
                    "symbol": "NIFTY",
                    "expiry": "27OCT2026",
                    "data": [{"strikePrice": 25000}],
                }

                failed = await fetcher_module._persist_option_chain_snapshot_if_due(
                    chain, "NIFTY", now_mono=2000.0
                )
                retried = await fetcher_module._persist_option_chain_snapshot_if_due(
                    chain, "NIFTY", now_mono=2000.1
                )

                assert failed is False
                assert retried is True
                assert calls == [
                    ("NIFTY", "27OCT2026"),
                    ("NIFTY", "27OCT2026"),
                ]
        finally:
            snapshot_persistence_enabled.reset(token)
            fetcher_module._OPTION_SNAPSHOT_PERSIST_LAST_SAVED = old_last
            fetcher_module._OPTION_SNAPSHOT_PERSIST_LOCK = old_lock

    asyncio.run(scenario())


def test_merge_live_option_ticks_preserves_ws_exchange_timestamp(monkeypatch):
    from app.services.angel_live_feed import angel_live_feed

    fetcher = DataFetcher()
    received_at = time.time()
    exchange_ts = "2026-10-13T11:25:25+05:30"
    exchange_ts_ms = 1791870925000

    snapshot = {
        "12345": {
            "symbol": "NIFTY",
            "expiry": "13OCT2026",
            "received_at": received_at,
            "ltp": 101.0,
            "bidprice": 100.5,
            "askPrice": 101.5,
            "timestamp": exchange_ts,
            "exchange_timestamp": exchange_ts_ms,
            "source": "angel_one_websocket",
        }
    }
    monkeypatch.setattr(
        angel_live_feed, "get_option_snapshot",
        lambda symbol=None: snapshot,
    )

    chain = {
        "data_source": "angel_one",
        "symbol": "NIFTY",
        "expiry": "13OCT2026",
        "data": [{
            "strikePrice": 22500,
            "CE": {
                "token": "12345",
                "lastPrice": 100.0,
                "openInterest": 123,
                "changeinOpenInterest": 15,
                "totalTradedVolume": 200,
                "exchangeFeedTime": "13-Oct-2026 11:24:00",
                "exchangeTradeTime": "13-Oct-2026 11:24:00",
            },
            "PE": {},
        }],
    }

    result = fetcher._merge_live_option_ticks(chain)
    assert result is not chain
    assert chain["data"][0]["CE"]["lastPrice"] == 100.0
    assert "live_ws" not in chain["data"][0]["CE"]
    leg = result["data"][0]["CE"]

    # The REST analytics fields must not be overwritten by WS values.
    assert leg["lastPrice"] == 100.0
    assert leg["openInterest"] == 123
    assert leg["changeinOpenInterest"] == 15
    assert leg["totalTradedVolume"] == 200
    assert leg["live_ws_quote"]["ltp"] == 101.0
    assert leg["live_ws_quote"]["bid"] == 100.5
    assert leg["live_ws_quote"]["ask"] == 101.5
    assert leg["live_ws_quote"]["source"] == "angel_one_websocket"
    assert leg["live_ws"] is True
    assert leg["live_ws_exchange_timestamp"] == exchange_ts
    assert leg["live_ws_exchange_timestamp_ms"] == exchange_ts_ms
    assert leg["exchangeFeedTime"] == "13-Oct-2026 11:24:00"
    assert leg["exchangeTradeTime"] == "13-Oct-2026 11:24:00"

def test_merge_clears_old_ws_markers_without_snapshot(monkeypatch):
    from app.services.angel_live_feed import angel_live_feed
    monkeypatch.setattr(angel_live_feed, "get_option_snapshot",
                        lambda symbol=None: {})
    chain = {"data_source": "angel_one", "symbol": "NIFTY",
             "live_option_ticks_merged": 1,
             "live_option_data_source": "angel_one_websocket",
             "data": [{"CE": {"live_ws": True, "live_ws_age_sec": 0.5},
                       "PE": {}}]}
    result = DataFetcher()._merge_live_option_ticks(chain)
    assert result is not chain
    assert result["live_option_ticks_merged"] == 0
    assert "live_option_data_source" not in result
    assert "live_ws" not in result["data"][0]["CE"]
    assert chain["data"][0]["CE"]["live_ws"] is True


def test_refresh_intraday_freshness_recomputes_cached_age(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services import data_fetcher as data_fetcher_module

    now = datetime(2026, 10, 14, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(data_fetcher_module, "current_ist", lambda: now)

    snapshot = {
        "latest_timestamp": "2026-10-14T10:00:00+05:30",
        "timestamps": ["2026-10-14T10:00:00+05:30"],
        "fresh": True,
        "freshness_minutes": 1.0,
    }
    refreshed = DataFetcher._refresh_intraday_freshness(
        snapshot, "FIVE_MINUTE"
    )

    assert refreshed["freshness_minutes"] == 30.0
    assert refreshed["fresh"] is False
    assert snapshot["freshness_minutes"] == 1.0  # input is not mutated


@pytest.mark.asyncio
async def test_last_good_snapshot_stores_proxy_enriched_volumes(monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from app.services import data_fetcher as data_fetcher_module

    now = datetime(2026, 10, 14, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(data_fetcher_module, "current_ist", lambda: now)
    monkeypatch.setattr(DataFetcher, "_LAST_GOOD_INTRADAY_SNAPSHOTS", {})

    session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    timestamps = [
        (session_start + timedelta(minutes=5 * i)).isoformat()
        for i in range(16)
    ]
    zeros = [0] * len(timestamps)
    snapshot = {
        "available": True,
        "interval": "FIVE_MINUTE",
        "data_source": "angel_one_intraday",
        "opens": [100.0] * len(timestamps),
        "highs": [102.0] * len(timestamps),
        "lows": [99.0] * len(timestamps),
        "closes": [101.0] * len(timestamps),
        "volumes": zeros,
        "timestamps": timestamps,
        "latest_timestamp": timestamps[-1],
        "bar_count": len(timestamps),
    }

    fetcher = DataFetcher()

    async def fake_snapshot(symbol, interval):
        return dict(snapshot)

    monkeypatch.setattr(
        fetcher, "_get_intraday_ohlc_snapshot", fake_snapshot
    )
    monkeypatch.setattr(
        fetcher,
        "_get_cached_futures_volumes",
        lambda symbol, ts, interval: [100 + i for i in range(len(ts))],
    )

    result = await fetcher.get_intraday_ohlc(
        "NIFTY", interval="FIVE_MINUTE", bars=16
    )
    cached = DataFetcher._LAST_GOOD_INTRADAY_SNAPSHOTS[
        ("NIFTY", "FIVE_MINUTE")
    ]["result"]

    assert result["volume_source"] == "front_month_futures_proxy"
    assert result["volumes"] == [100 + i for i in range(16)]
    assert cached["volume_source"] == "front_month_futures_proxy"
    assert cached["volumes"] == [100 + i for i in range(16)]
    assert cached["fresh"] is True


@pytest.mark.asyncio
async def test_intraday_proxy_applies_when_only_historical_volume_exists(monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from app.services import data_fetcher as data_fetcher_module

    now = datetime(2026, 10, 14, 10, 35, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(data_fetcher_module, "current_ist", lambda: now)
    monkeypatch.setattr(DataFetcher, "_LAST_GOOD_INTRADAY_SNAPSHOTS", {})

    old_ts = _coverage_timestamps("2026-10-13", 2)
    session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    new_ts = [
        (session_start + timedelta(minutes=5 * i)).isoformat()
        for i in range(16)
    ]
    timestamps = old_ts + new_ts
    old_volumes = [701, 702] + [0] * len(new_ts)
    proxy = [901, 902] + [100 + i for i in range(len(new_ts))]
    snapshot = {
        "available": True,
        "interval": "FIVE_MINUTE",
        "data_source": "angel_one_intraday",
        "opens": [100.0] * len(timestamps),
        "highs": [102.0] * len(timestamps),
        "lows": [99.0] * len(timestamps),
        "closes": [101.0] * len(timestamps),
        "volumes": old_volumes,
        "timestamps": timestamps,
        "latest_timestamp": timestamps[-1],
        "bar_count": len(timestamps),
    }
    fetcher = DataFetcher()

    async def fake_snapshot(symbol, interval):
        return dict(snapshot)

    monkeypatch.setattr(fetcher, "_get_intraday_ohlc_snapshot", fake_snapshot)
    monkeypatch.setattr(
        fetcher, "_get_cached_futures_volumes",
        lambda symbol, ts, interval: proxy,
    )

    result = await fetcher.get_intraday_ohlc(
        "BANKNIFTY", interval="FIVE_MINUTE", bars=len(timestamps)
    )
    assert result["volumes"][:2] == [701, 702]
    assert result["volumes"][2:] == proxy[2:]
    assert result["volume_source"] == "front_month_futures_proxy"


def test_latest_session_volume_detection_ignores_historical_volume():
    fetcher = DataFetcher()
    timestamps = (
        _coverage_timestamps("2026-10-13", 3)
        + _coverage_timestamps("2026-10-14", 4)
    )
    volumes = [100, 200, 300, 0, 0, 0, 0]
    assert not fetcher._has_positive_volume_in_latest_session(
        volumes, timestamps, "FIVE_MINUTE"
    )
    volumes[-1] = 40
    assert fetcher._has_positive_volume_in_latest_session(
        volumes, timestamps, "FIVE_MINUTE"
    )


def test_latest_session_proxy_merge_preserves_prior_session_volume():
    fetcher = DataFetcher()
    timestamps = (
        _coverage_timestamps("2026-10-13", 3)
        + _coverage_timestamps("2026-10-14", 4)
    )
    volumes = [100, 200, 300, 0, 0, 0, 0]
    proxy = [900, 901, 902, 101, 102, 103, 104]
    merged = fetcher._merge_latest_session_proxy_volumes(
        volumes, timestamps, proxy, "FIVE_MINUTE"
    )
    assert merged == [100, 200, 300, 101, 102, 103, 104]
    assert volumes == [100, 200, 300, 0, 0, 0, 0]

def test_futures_volume_cache_ttl_tolerates_refresh_delay(monkeypatch):
    import time

    fetcher = DataFetcher()
    timestamps = _coverage_timestamps("2026-10-09", 10)
    normalize = DataFetcher._candle_timestamp_key
    key = ("FINNIFTY", "FIVE_MINUTE")
    cached = {
        normalize(ts): 100 + i for i, ts in enumerate(timestamps)
    }
    now = time.time()

    monkeypatch.setattr(DataFetcher, "_FUTURES_VOLUME_CACHE", {key: cached})
    monkeypatch.setattr(
        DataFetcher,
        "_FUTURES_VOLUME_CACHE_TS",
        {key: now - 200},
    )

    assert DataFetcher._FUTURES_VOLUME_CACHE_TTL == 360.0
    assert fetcher._get_cached_futures_volumes(
        "FINNIFTY", timestamps, "FIVE_MINUTE"
    ) == [100 + i for i in range(10)]

    DataFetcher._FUTURES_VOLUME_CACHE_TS[key] = time.time() - 361
    assert fetcher._get_cached_futures_volumes(
        "FINNIFTY", timestamps, "FIVE_MINUTE"
    ) is None

@pytest.mark.asyncio
async def test_futures_proxy_refresh_forces_background_priority(monkeypatch):
    from types import SimpleNamespace
    import app.services.angel_one as angel_one_module

    observed_priorities = []

    async def fake_get_futures_candle_data(
        symbol, interval, from_date, to_date
    ):
        observed_priorities.append(
            angel_one_module.get_angel_call_priority()
        )
        return [
            {
                "timestamp": "2026-10-14T09:15:00+05:30",
                "volume": 123,
            },
            {
                "timestamp": "2026-10-14T09:20:00+05:30",
                "volume": 234,
            },
        ]

    fake_session = SimpleNamespace(
        is_configured=True,
        get_futures_candle_data=fake_get_futures_candle_data,
    )
    monkeypatch.setattr(angel_one_module, "angel_session", fake_session)
    monkeypatch.setattr(DataFetcher, "_FUTURES_VOLUME_CACHE", {})
    monkeypatch.setattr(DataFetcher, "_FUTURES_VOLUME_CACHE_TS", {})

    original_token = angel_one_module.set_angel_call_priority(
        angel_one_module.ANGEL_PRIORITY_NORMAL
    )
    try:
        fetcher = DataFetcher()
        await fetcher._refresh_futures_volume_proxy(
            "NIFTY",
            "FIVE_MINUTE",
            "2026-10-14 09:15",
            "2026-10-14 10:00",
        )
        assert (
            angel_one_module.get_angel_call_priority()
            == angel_one_module.ANGEL_PRIORITY_NORMAL
        )
    finally:
        angel_one_module.reset_angel_call_priority(original_token)

    assert observed_priorities == [angel_one_module.ANGEL_PRIORITY_BACKGROUND]
    cache_key = ("NIFTY", "FIVE_MINUTE")
    assert DataFetcher._FUTURES_VOLUME_CACHE[cache_key]
    assert DataFetcher._FUTURES_VOLUME_CACHE_TS[cache_key] > 0


def test_get_option_chain_persists_rest_before_ws_overlay(monkeypatch):
    import asyncio
    from app.services import data_fetcher as module

    fetcher = DataFetcher()
    base = {
        "symbol": "NIFTY",
        "expiry": "27OCT2026",
        "data_source": "angel_one",
        "data": [{"strikePrice": 25000, "CE": {"lastPrice": 100.0}}],
    }
    merged = {**base, "live_option_data_source": "angel_one_websocket"}
    events = []

    async def fake_cached(symbol, expiry=None, strikes_each_side=10):
        return base

    async def fake_persist(chain, symbol):
        events.append(("persist", chain is base, symbol))
        return True

    def fake_merge(chain):
        events.append(("merge", chain is base))
        return merged

    monkeypatch.setattr(fetcher, "_get_option_chain_cached", fake_cached)
    monkeypatch.setattr(module, "_persist_option_chain_snapshot_if_due", fake_persist)
    monkeypatch.setattr(fetcher, "_merge_live_option_ticks", fake_merge)

    async def run():
        result = await fetcher.get_option_chain("NIFTY")
        assert result is merged

    asyncio.run(run())
    assert events == [("persist", True, "NIFTY"), ("merge", True)]
