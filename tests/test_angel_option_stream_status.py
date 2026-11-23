import threading

from app.services.angel_live_feed import AngelLiveFeed


def make_feed():
    feed = object.__new__(AngelLiveFeed)
    feed._lock = threading.Lock()
    feed._option_tokens_by_symbol = {
        symbol: {symbol + "-TOKEN"}
        for symbol in feed.SYMBOL_TOKENS
    }
    feed._option_ticks = {}
    return feed


def test_option_stream_status_reports_all_symbols_and_empty_ticks():
    feed = make_feed()

    result = feed._option_stream_status(now=1000.0)

    assert set(result) == {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}
    for symbol, status in result.items():
        assert status == {
            "subscribed_tokens": 1,
            "tick_count": 0,
            "latest_tick_age_sec": None,
            "latest_exchange_age_sec": None,
            "fresh": False,
        }


def test_option_stream_status_marks_recent_exchange_tick_fresh_per_symbol():
    feed = make_feed()
    now = 1000.0
    for symbol in feed.SYMBOL_TOKENS:
        token = symbol + "-TOKEN"
        feed._option_ticks[token] = {
            "symbol": symbol,
            "received_at": now - 2.0,
            "exchange_timestamp": int((now - 3.0) * 1000),
        }

    result = feed._option_stream_status(now=now)

    assert all(item["tick_count"] == 1 for item in result.values())
    assert all(item["latest_tick_age_sec"] == 2.0 for item in result.values())
    assert all(item["latest_exchange_age_sec"] == 3.0 for item in result.values())
    assert all(item["fresh"] is True for item in result.values())


def test_option_stream_status_rejects_stale_or_missing_exchange_timestamp():
    feed = make_feed()
    now = 1000.0
    feed._option_ticks = {
        "NIFTY-TOKEN": {
            "symbol": "NIFTY",
            "received_at": now - 11.0,
            "exchange_timestamp": int((now - 11.0) * 1000),
        },
        "BANKNIFTY-TOKEN": {
            "symbol": "BANKNIFTY",
            "received_at": now - 1.0,
        },
    }

    result = feed._option_stream_status(now=now)

    assert result["NIFTY"]["fresh"] is False
    assert result["BANKNIFTY"]["fresh"] is False
    assert result["BANKNIFTY"]["latest_exchange_age_sec"] is None
    assert result["FINNIFTY"]["fresh"] is False
    assert result["SENSEX"]["fresh"] is False


def test_option_stream_status_uses_latest_tick_for_each_symbol():
    feed = make_feed()
    now = 1000.0
    feed._option_ticks = {
        "NIFTY-OLD": {
            "symbol": "NIFTY",
            "received_at": now - 20.0,
            "exchange_timestamp": int((now - 20.0) * 1000),
        },
        "NIFTY-NEW": {
            "symbol": "NIFTY",
            "received_at": now - 1.0,
            "exchange_timestamp": int((now - 2.0) * 1000),
        },
    }

    result = feed._option_stream_status(now=now)

    assert result["NIFTY"]["tick_count"] == 2
    assert result["NIFTY"]["latest_tick_age_sec"] == 1.0
    assert result["NIFTY"]["fresh"] is True
