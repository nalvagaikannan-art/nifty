import asyncio
import threading

from app.services.angel_live_feed import AngelLiveFeed
from app.services.angel_one import angel_session


class FakeWebSocket:
    def __init__(self, fail_unsubscribe=False, fail_subscribe=False):
        self.fail_unsubscribe = fail_unsubscribe
        self.fail_subscribe = fail_subscribe
        self.calls = []

    def unsubscribe(self, *args):
        self.calls.append(("unsubscribe", args))
        if self.fail_unsubscribe:
            raise RuntimeError("SIMULATED_UNSUBSCRIBE_FAILURE")

    def subscribe(self, *args):
        self.calls.append(("subscribe", args))
        if self.fail_subscribe:
            raise RuntimeError("SIMULATED_SUBSCRIBE_FAILURE")


def make_feed(monkeypatch, ws):
    feed = object.__new__(AngelLiveFeed)
    feed._lock = threading.Lock()

    # Mirror the constructor-owned lifecycle and pending-send state.
    # object.__new__ deliberately bypasses AngelLiveFeed.__init__().
    feed._stop_event = threading.Event()
    feed._option_refresh_stop = threading.Event()
    feed._option_pending_sends_lock = threading.Lock()
    feed._option_pending_sends = set()

    feed._ticks = {}
    feed._option_ticks = {}
    feed._option_token_meta = {}
    feed._option_tokens_by_symbol = {}
    feed._option_last_error = None
    feed._option_last_refresh_at = 0.0
    feed._ws = ws

    for symbol in feed.SYMBOL_TOKENS:
        old_token = "old-" + symbol
        feed._option_tokens_by_symbol[symbol] = {old_token}
        feed._option_token_meta[old_token] = {
            "symbol": symbol,
            "token": old_token,
            "expiry": "27OCT2026",
        }
        feed._option_ticks[old_token] = {
            "symbol": symbol,
            "token": old_token,
            "ltp": 100.0,
        }

    async def fake_resolver(
        symbol, strikes_each_side=10, spot_price=None
    ):
        return [{
            "symbol": symbol,
            "token": "new-" + symbol,
            "expiry": "27OCT2026",
            "strike": 100.0,
            "optionType": "CE",
            "tradingsymbol": symbol + "TESTCE",
        }]

    monkeypatch.setattr(
        angel_session, "resolve_option_tokens", fake_resolver
    )
    return feed


def test_option_subscription_preserves_old_tokens_when_unsubscribe_fails(
    monkeypatch,
):
    feed = make_feed(
        monkeypatch, FakeWebSocket(fail_unsubscribe=True)
    )

    asyncio.run(feed._refresh_option_subscriptions())

    for symbol in feed.SYMBOL_TOKENS:
        old_token = "old-" + symbol
        new_token = "new-" + symbol

        assert old_token in feed._option_tokens_by_symbol[symbol]
        assert new_token in feed._option_tokens_by_symbol[symbol]
        assert old_token in feed._option_token_meta
        assert new_token in feed._option_token_meta
        assert old_token in feed._option_ticks


def test_option_subscription_does_not_retrack_removed_tokens_when_new_subscribe_fails(
    monkeypatch,
):
    feed = make_feed(
        monkeypatch, FakeWebSocket(fail_subscribe=True)
    )

    asyncio.run(feed._refresh_option_subscriptions())

    for symbol in feed.SYMBOL_TOKENS:
        old_token = "old-" + symbol
        new_token = "new-" + symbol

        assert feed._option_tokens_by_symbol[symbol] == set()
        assert old_token not in feed._option_token_meta
        assert old_token not in feed._option_ticks
        assert new_token not in feed._option_token_meta


def test_option_subscription_successfully_replaces_old_tokens(monkeypatch):
    feed = make_feed(monkeypatch, FakeWebSocket())

    asyncio.run(feed._refresh_option_subscriptions())

    for symbol in feed.SYMBOL_TOKENS:
        old_token = "old-" + symbol
        new_token = "new-" + symbol

        assert feed._option_tokens_by_symbol[symbol] == {new_token}
        assert old_token not in feed._option_token_meta
        assert old_token not in feed._option_ticks
        assert new_token in feed._option_token_meta
