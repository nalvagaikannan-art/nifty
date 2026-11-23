import asyncio

from app import main


class FakeFeed:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def start(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_startup_retry_retries_false_result_and_stops_after_success(monkeypatch):
    feed = FakeFeed([False, True])

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(main, "is_market_hours_ist", lambda: True)
    monkeypatch.setattr(main.asyncio, "sleep", no_wait)

    asyncio.run(main._start_angel_live_feed_with_retry(feed))

    assert feed.calls == 2


def test_startup_retry_recovers_from_transient_exception(monkeypatch):
    feed = FakeFeed([RuntimeError("temporary login failure"), True])

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(main, "is_market_hours_ist", lambda: True)
    monkeypatch.setattr(main.asyncio, "sleep", no_wait)

    asyncio.run(main._start_angel_live_feed_with_retry(feed))

    assert feed.calls == 2


def test_startup_retry_does_not_attempt_login_outside_market_hours(monkeypatch):
    feed = FakeFeed([True])

    async def cancel_wait(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "is_market_hours_ist", lambda: False)
    monkeypatch.setattr(main.asyncio, "sleep", cancel_wait)

    try:
        asyncio.run(main._start_angel_live_feed_with_retry(feed))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("retry loop should remain cancellable")

    assert feed.calls == 0
