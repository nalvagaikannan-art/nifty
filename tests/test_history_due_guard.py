import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services import history_service


class FakeResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class FakeSession:
    def __init__(self, row):
        self.row = row

    async def execute(self, query):
        return FakeResult(self.row)


class FakeContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return False


def setup_db(monkeypatch, row, now):
    monkeypatch.setattr(
        history_service, "AsyncSessionLocal",
        lambda: FakeContext(FakeSession(row))
    )
    monkeypatch.setattr(history_service, "now_utc_naive", lambda: now)


def test_due_when_no_previous_row(monkeypatch):
    now = datetime(2026, 10, 12, 6, 0)
    setup_db(monkeypatch, None, now)
    assert asyncio.run(
        history_service.analysis_history_due("NIFTY", "new")
    ) is True


def test_not_due_within_five_minutes(monkeypatch):
    now = datetime(2026, 10, 12, 6, 0)
    row = SimpleNamespace(
        timestamp=now - timedelta(minutes=2),
        result={"market_snapshot_timestamp": "old"},
    )
    setup_db(monkeypatch, row, now)
    assert asyncio.run(
        history_service.analysis_history_due("NIFTY", "new")
    ) is False


def test_not_due_for_same_snapshot(monkeypatch):
    now = datetime(2026, 10, 12, 6, 0)
    row = SimpleNamespace(
        timestamp=now - timedelta(minutes=10),
        result={"market_snapshot_timestamp": "same"},
    )
    setup_db(monkeypatch, row, now)
    assert asyncio.run(
        history_service.analysis_history_due("NIFTY", "same")
    ) is False


def test_due_after_interval_for_new_snapshot(monkeypatch):
    now = datetime(2026, 10, 12, 6, 0)
    row = SimpleNamespace(
        timestamp=now - timedelta(minutes=8),
        result={"market_snapshot_timestamp": "old"},
    )
    setup_db(monkeypatch, row, now)
    assert asyncio.run(
        history_service.analysis_history_due("NIFTY", "new")
    ) is True


def test_database_error_fails_closed(monkeypatch):
    def fail():
        raise RuntimeError("simulated DB error")

    monkeypatch.setattr(history_service, "AsyncSessionLocal", fail)
    assert asyncio.run(
        history_service.analysis_history_due("NIFTY", "new")
    ) is False
