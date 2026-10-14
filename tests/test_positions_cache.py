import asyncio

import pytest

from app.api.routes import positions


@pytest.fixture(autouse=True)
def reset_positions_cache():
    positions._positions_cache = None
    positions._positions_refresh_task = None
    yield
    positions._positions_cache = None
    positions._positions_refresh_task = None


class FakeAngel:
    def __init__(self, positions_data=None, error=None):
        self.calls = 0
        self.positions_data = positions_data or [
            {
                "tradingsymbol": "NIFTYTEST",
                "netqty": 1,
                "lasttradedprice": 100.0,
            }
        ]
        self.error = error

    async def get_positions(self):
        self.calls += 1
        await asyncio.sleep(0.02)
        if self.error is not None:
            raise self.error
        return self.positions_data


def test_concurrent_requests_share_one_broker_call_and_copies_are_isolated():
    async def run():
        angel = FakeAngel()
        snapshots = await asyncio.gather(
            *(positions._get_positions_snapshot(angel) for _ in range(8))
        )

        assert angel.calls == 1
        assert all(item == snapshots[0] for item in snapshots)

        snapshots[0][0]["lasttradedprice"] = 999.0
        next_snapshot = await positions._get_positions_snapshot(angel)
        assert angel.calls == 1
        assert next_snapshot[0]["lasttradedprice"] == 100.0

    asyncio.run(run())


def test_expired_snapshot_triggers_new_broker_call(monkeypatch):
    monkeypatch.setattr(positions, "_POSITIONS_CACHE_TTL_SECONDS", 0.0)

    async def run():
        angel = FakeAngel()
        first = await positions._get_positions_snapshot(angel)
        second = await positions._get_positions_snapshot(angel)

        assert first == second
        assert angel.calls == 2

    asyncio.run(run())


def test_broker_errors_are_not_cached():
    async def run():
        angel = FakeAngel(error=positions.AngelOneError("test rate limit"))

        with pytest.raises(positions.AngelOneError, match="test rate limit"):
            await positions._get_positions_snapshot(angel)

        with pytest.raises(positions.AngelOneError, match="test rate limit"):
            await positions._get_positions_snapshot(angel)

        assert angel.calls == 2

    asyncio.run(run())


def test_cancelled_request_does_not_cancel_shared_refresh():
    class BlockingAngel:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def get_positions(self):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return [{
                "tradingsymbol": "NIFTYTEST",
                "netqty": 1,
                "lasttradedprice": 100.0,
            }]

    async def run():
        angel = BlockingAngel()
        cancelled_request = asyncio.create_task(
            positions._get_positions_snapshot(angel)
        )
        surviving_request = None

        try:
            await asyncio.wait_for(angel.started.wait(), timeout=2.0)

            surviving_request = asyncio.create_task(
                positions._get_positions_snapshot(angel)
            )
            # Let the second caller join the in-flight refresh.
            await asyncio.sleep(0)

            cancelled_request.cancel()
            result = await asyncio.gather(
                cancelled_request, return_exceptions=True
            )
            assert isinstance(result[0], asyncio.CancelledError)

            angel.release.set()
            snapshot = await asyncio.wait_for(
                surviving_request, timeout=2.0
            )

            assert angel.calls == 1
            assert snapshot[0]["lasttradedprice"] == 100.0
        finally:
            angel.release.set()
            for task in (cancelled_request, surviving_request):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                cancelled_request,
                *( [surviving_request] if surviving_request is not None else [] ),
                return_exceptions=True,
            )

    asyncio.run(run())
