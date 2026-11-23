import asyncio
from types import SimpleNamespace

import pytest

from app.services import history_collector


@pytest.fixture(autouse=True)
def _disable_option_chain_foreground_grace_for_unit_tests(monkeypatch):
    # Keep existing scheduler tests fast; the dedicated regression test
    # below verifies the configured wait and its order relative to fetching.
    monkeypatch.setattr(
        history_collector, 'OPTION_CHAIN_SNAPSHOT_FOREGROUND_GRACE_SEC', 0.0
    )


class FakeFetcher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def get_option_chain(self, symbol, expiry=None):
        self.calls.append((symbol, expiry))
        if self.error:
            raise self.error
        return self.result


def test_option_chain_poll_uses_shared_fetcher(monkeypatch):
    monkeypatch.setattr(history_collector, "is_market_hours_ist", lambda: True)
    fetcher = FakeFetcher({"data": [{"strikePrice": 25000}]})
    analyzer = SimpleNamespace(fetcher=fetcher)

    result = asyncio.run(
        history_collector._collect_option_chain_snapshot_once(analyzer, "NIFTY")
    )

    assert result is True
    assert fetcher.calls == [("NIFTY", None)]


def test_option_chain_poll_gives_foreground_request_head_start(monkeypatch):
    from app.services import angel_one

    monkeypatch.setattr(history_collector, "is_market_hours_ist", lambda: True)
    monkeypatch.setattr(
        history_collector, "OPTION_CHAIN_SNAPSHOT_FOREGROUND_GRACE_SEC", 2.0
    )
    observed = []

    async def fake_sleep(seconds):
        observed.append(("grace", seconds))

    monkeypatch.setattr(history_collector.asyncio, "sleep", fake_sleep)

    class OrderedFetcher:
        async def get_option_chain(self, symbol, expiry=None):
            observed.append((
                "fetch", symbol, expiry, angel_one.get_angel_call_priority()
            ))
            return {"data": [{"strikePrice": 25000}]}

    result = asyncio.run(
        history_collector._collect_option_chain_snapshot_once(
            SimpleNamespace(fetcher=OrderedFetcher()), "NIFTY"
        )
    )

    assert result is True
    assert observed == [
        ("grace", 2.0),
        ("fetch", "NIFTY", None, angel_one.ANGEL_PRIORITY_BACKGROUND),
    ]


def test_option_chain_poll_skips_when_market_closed(monkeypatch):
    monkeypatch.setattr(history_collector, "is_market_hours_ist", lambda: False)
    fetcher = FakeFetcher({"data": [{"strikePrice": 25000}]})
    analyzer = SimpleNamespace(fetcher=fetcher)

    result = asyncio.run(
        history_collector._collect_option_chain_snapshot_once(analyzer, "NIFTY")
    )

    assert result is False
    assert fetcher.calls == []


def test_option_chain_poll_rejects_empty_chain(monkeypatch):
    monkeypatch.setattr(history_collector, "is_market_hours_ist", lambda: True)
    fetcher = FakeFetcher({"data": []})
    analyzer = SimpleNamespace(fetcher=fetcher)

    result = asyncio.run(
        history_collector._collect_option_chain_snapshot_once(analyzer, "FINNIFTY")
    )

    assert result is False
    assert fetcher.calls == [("FINNIFTY", None)]


def test_option_chain_poll_handles_fetch_error(monkeypatch):
    monkeypatch.setattr(history_collector, "is_market_hours_ist", lambda: True)
    fetcher = FakeFetcher(error=RuntimeError("simulated failure"))
    analyzer = SimpleNamespace(fetcher=fetcher)

    result = asyncio.run(
        history_collector._collect_option_chain_snapshot_once(analyzer, "SENSEX")
    )

    assert result is False
    assert fetcher.calls == [("SENSEX", None)]

# OPTION_CHAIN_BACKGROUND_PRIORITY_TESTS_20261016
def test_option_chain_snapshot_uses_background_priority_and_restores(monkeypatch):
    from app.services import angel_one

    observed = []

    class PriorityFetcher:
        async def get_option_chain(self, symbol, expiry=None):
            observed.append(angel_one.get_angel_call_priority())
            return {"data": [{"strikePrice": 25000}]}

    async def scenario():
        token = angel_one.set_angel_call_priority(
            angel_one.ANGEL_PRIORITY_NORMAL
        )
        try:
            result = await history_collector._collect_option_chain_snapshot_once(
                SimpleNamespace(fetcher=PriorityFetcher()), "NIFTY"
            )
            assert result is True
            assert observed == [angel_one.ANGEL_PRIORITY_BACKGROUND]
            assert (
                angel_one.get_angel_call_priority()
                == angel_one.ANGEL_PRIORITY_NORMAL
            )
        finally:
            angel_one.reset_angel_call_priority(token)

    monkeypatch.setattr(
        history_collector, "is_market_hours_ist", lambda: True
    )
    asyncio.run(scenario())


def test_option_chain_snapshot_restores_priority_after_fetch_error(monkeypatch):
    from app.services import angel_one

    observed = []

    class FailingPriorityFetcher:
        async def get_option_chain(self, symbol, expiry=None):
            observed.append(angel_one.get_angel_call_priority())
            raise RuntimeError("simulated snapshot failure")

    async def scenario():
        token = angel_one.set_angel_call_priority(
            angel_one.ANGEL_PRIORITY_NORMAL
        )
        try:
            result = await history_collector._collect_option_chain_snapshot_once(
                SimpleNamespace(fetcher=FailingPriorityFetcher()), "SENSEX"
            )
            assert result is False
            assert observed == [angel_one.ANGEL_PRIORITY_BACKGROUND]
            assert (
                angel_one.get_angel_call_priority()
                == angel_one.ANGEL_PRIORITY_NORMAL
            )
        finally:
            angel_one.reset_angel_call_priority(token)

    monkeypatch.setattr(
        history_collector, "is_market_hours_ist", lambda: True
    )
    asyncio.run(scenario())

