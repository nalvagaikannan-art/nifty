import asyncio

from app.api.routes import angel
from app.schemas import AngelLiveFeedResponse


def test_live_feed_route_preserves_per_symbol_option_stream_diagnostics(monkeypatch):
    by_symbol = {
        symbol: {
            "subscribed_tokens": 20,
            "tick_count": 4,
            "latest_tick_age_sec": 1.0,
            "latest_exchange_age_sec": 2.0,
            "fresh": True,
        }
        for symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX")
    }
    websocket_status = {
        "connected": True,
        "option_stream": {
            "tick_count": 16,
            "by_symbol": by_symbol,
        },
    }

    monkeypatch.setattr(angel.angel_live_feed, "status", lambda: websocket_status)
    monkeypatch.setattr(angel.angel_live_feed, "get_snapshot", lambda: {})

    payload = asyncio.run(angel.get_live_feed_status())
    response = AngelLiveFeedResponse.model_validate(payload)

    assert response.websocket["option_stream"]["by_symbol"] == by_symbol
    assert set(response.websocket["option_stream"]["by_symbol"]) == {
        "NIFTY",
        "BANKNIFTY",
        "FINNIFTY",
        "SENSEX",
    }
