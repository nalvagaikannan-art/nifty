import asyncio
from datetime import datetime


def test_concurrent_ai_history_saves_serialize_check_and_write(monkeypatch):
    from app.services import history_service as hs

    fixed_now = datetime(2026, 10, 12, 7, 45, 0)
    rows = []

    class FakeResult:
        def scalar_one_or_none(self):
            return rows[-1] if rows else None

        def scalars(self):
            return self

        def all(self):
            return list(rows)

    class FakeSession:
        def __init__(self):
            self.pending = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, statement):
            # Yield at DB read boundaries to reproduce overlapping requests.
            await asyncio.sleep(0)
            return FakeResult()

        def add(self, row):
            self.pending.append(row)

        async def commit(self):
            # Yield before commit so concurrent callers can overlap unless
            # the shared save lock protects the complete check-write section.
            await asyncio.sleep(0)
            for row in self.pending:
                row.timestamp = fixed_now
                rows.append(row)
            self.pending.clear()

    monkeypatch.setattr(hs, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(hs, "now_utc_naive", lambda: fixed_now)

    payload = {
        "market_snapshot_timestamp": "2026-10-12T13:15:00+05:30",
        "signal_action": "WAIT",
    }

    async def run_concurrent_saves():
        await asyncio.gather(
            hs.save_analysis_result("NIFTY", "ai", dict(payload)),
            hs.save_analysis_result("NIFTY", "ai", dict(payload)),
        )

    asyncio.run(run_concurrent_saves())

    assert len(rows) == 1, (
        f"expected one persisted row from concurrent saves; got {len(rows)}"
    )
    assert rows[0].symbol == "NIFTY"
    assert rows[0].analysis_type == "ai"
    assert rows[0].result["market_snapshot_timestamp"] == (
        "2026-10-12T13:15:00+05:30"
    )
