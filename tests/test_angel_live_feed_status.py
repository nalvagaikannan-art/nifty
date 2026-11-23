import app.services.angel_live_feed as live_feed_module


def test_status_requires_received_and_exchange_timestamps_to_be_fresh(monkeypatch):
    feed = live_feed_module.AngelLiveFeed()
    monkeypatch.setattr(live_feed_module.time, "time", lambda: 1_000.0)

    # One future timestamp must never be reported as a fresh live tick.
    feed._ticks["NIFTY"] = {"symbol": "NIFTY", "ltp": 25000.0}
    feed._last_tick_received_at["NIFTY"] = 1_001.0
    feed._last_exchange_timestamp["NIFTY"] = 1_000.0

    feed._ticks["BANKNIFTY"] = {"symbol": "BANKNIFTY", "ltp": 55000.0}
    feed._last_tick_received_at["BANKNIFTY"] = 1_000.0
    feed._last_exchange_timestamp["BANKNIFTY"] = 1_001.0

    # A timestamp just beyond the freshness limit must not round down to 10.0.
    feed._ticks["FINNIFTY"] = {"symbol": "FINNIFTY", "ltp": 26000.0}
    feed._last_tick_received_at["FINNIFTY"] = 989.96
    feed._last_exchange_timestamp["FINNIFTY"] = 990.0

    # Both timestamps inside the strict 0..10 second window are fresh.
    feed._ticks["SENSEX"] = {"symbol": "SENSEX", "ltp": 80000.0}
    feed._last_tick_received_at["SENSEX"] = 995.0
    feed._last_exchange_timestamp["SENSEX"] = 996.0

    status = feed.status()

    assert status["symbols_with_ticks"] == 4
    assert status["fresh_tick_symbols"] == ["SENSEX"]
    assert status["symbol_status"]["NIFTY"]["has_tick"] is True
    assert status["symbol_status"]["NIFTY"]["fresh"] is False
    assert status["symbol_status"]["BANKNIFTY"]["fresh"] is False
    assert status["symbol_status"]["FINNIFTY"]["fresh"] is False
    assert status["symbol_status"]["SENSEX"]["fresh"] is True


def test_status_reports_missing_ticks_for_all_four_symbols(monkeypatch):
    feed = live_feed_module.AngelLiveFeed()
    monkeypatch.setattr(live_feed_module.time, "time", lambda: 2_000.0)

    status = feed.status()

    assert status["symbols_with_ticks"] == 0
    assert status["fresh_tick_symbols"] == []
    assert set(status["symbol_status"]) == {
        "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"
    }
    assert all(
        details["has_tick"] is False and details["fresh"] is False
        for details in status["symbol_status"].values()
    )
