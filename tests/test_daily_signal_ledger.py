
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.services.strategy_history as strategy_history
from app.database import Base
from app.models import DailySignalLedger


@pytest.fixture
async def ledger_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(strategy_history, "AsyncSessionLocal", Session)

    yield Session

    await engine.dispose()


@pytest.mark.asyncio
async def test_actionable_signal_captured_with_mtf_and_null_outcome(ledger_session):
    result = await strategy_history.record_daily_signal_ledger(
        symbol="NIFTY",
        action="BUY CE",
        option_type="CE",
        strike=22600,
        expiry="06-Oct-2026",
        spot=22562.7,
        option_ltp_snapshot=142.5,
        entry_price=143.2,
        signal_strength=72,
        confidence=68,
        lifecycle="CONFIRMED_CALL",
        confirmations=3,
        market_snapshot_timestamp="2026-10-05T12:15:00+05:30",
        technical_data_source="intraday_5min_ohlc",
        confluence={"quality": "HIGH", "score": 8},
        mtf_freshness={
            "5min": {"fresh": True, "freshness_minutes": 2.0},
            "15min": {"fresh": True, "freshness_minutes": 5.0},
            "1hr": {"fresh": True, "freshness_minutes": 12.0},
        },
        entry_snapshot={
            "strike": 22600,
            "ltp": 142.5,
            "entry_price": 143.2,
            "lot_size": 65,
        },
        lifecycle_confirmation_at="2026-10-05T12:10:00+05:30",
    )

    assert result is not None
    assert result["duplicate"] is False

    async with ledger_session() as db:
        row = (
            await db.execute(
                select(DailySignalLedger)
                .where(DailySignalLedger.id == result["id"])
            )
        ).scalar_one()

    assert row.symbol == "NIFTY"
    assert row.action == "BUY CE"
    assert row.option_type == "CE"
    assert row.strike == 22600
    assert row.entry_price == pytest.approx(143.2)
    assert row.option_ltp_snapshot == pytest.approx(142.5)
    assert row.mtf_freshness["5min"]["fresh"] is True
    assert row.mtf_freshness["15min"]["fresh"] is True
    assert row.mtf_freshness["1hr"]["fresh"] is True
    assert row.confluence["quality"] == "HIGH"
    assert row.entry_snapshot["lot_size"] == 65
    assert row.outcome is None


@pytest.mark.asyncio
async def test_duplicate_same_signal_episode_is_not_inserted_twice(ledger_session):
    kwargs = dict(
        symbol="BANKNIFTY",
        action="BUY PE",
        option_type="PE",
        strike=51000,
        expiry="06-Oct-2026",
        spot=51120,
        option_ltp_snapshot=185,
        entry_price=187,
        signal_strength=70,
        confidence=66,
        lifecycle="HOLD_PUT",
        confirmations=3,
        market_snapshot_timestamp="2026-10-05T13:00:00+05:30",
        technical_data_source="intraday_5min_ohlc",
        confluence={"quality": "HIGH"},
        mtf_freshness={"5min": {"fresh": True}},
        entry_snapshot={"strike": 51000, "entry_price": 187},
        lifecycle_confirmation_at="2026-10-05T12:50:00+05:30",
    )

    first = await strategy_history.record_daily_signal_ledger(**kwargs)
    second = await strategy_history.record_daily_signal_ledger(**kwargs)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["id"] == first["id"]

    async with ledger_session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(DailySignalLedger)
            )
        ).scalar_one()

    assert count == 1


def _install_ledger_integrity_race_sessions(monkeypatch, *, competitor_exists):
    inserted_rows = []
    sessions = []

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    class FakeSession:
        def __init__(self, index):
            self.index = index

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            # First session represents the initial SELECT before the insert.
            if self.index == 0:
                return FakeResult(None)

            # The second session represents the recovery re-query.
            if competitor_exists and inserted_rows:
                return FakeResult(SimpleNamespace(
                    id=777,
                    ledger_key=inserted_rows[0].ledger_key,
                ))
            return FakeResult(None)

        def add(self, row):
            inserted_rows.append(row)

        async def commit(self):
            raise IntegrityError(
                "INSERT INTO daily_signal_ledger",
                {},
                RuntimeError(
                    "UNIQUE constraint failed: daily_signal_ledger.ledger_key"
                ),
            )

    def factory():
        session = FakeSession(len(sessions))
        sessions.append(session)
        return session

    monkeypatch.setattr(strategy_history, "AsyncSessionLocal", factory)
    return sessions, inserted_rows


def _ledger_integrity_race_kwargs():
    return {
        "symbol": "BANKNIFTY",
        "action": "BUY PE",
        "option_type": "PE",
        "strike": 51000,
        "expiry": "06-Oct-2026",
        "spot": 51120,
        "lifecycle": "CONFIRMED_PUT",
        "confirmations": 3,
        "market_snapshot_timestamp": "2026-10-05T13:00:00+05:30",
        "lifecycle_confirmation_at": "2026-10-05T12:50:00+05:30",
    }


@pytest.mark.asyncio
async def test_integrity_race_requery_returns_duplicate(monkeypatch):
    sessions, inserted_rows = _install_ledger_integrity_race_sessions(
        monkeypatch, competitor_exists=True
    )

    result = await strategy_history.record_daily_signal_ledger(
        **_ledger_integrity_race_kwargs()
    )

    assert len(sessions) == 2
    assert len(inserted_rows) == 1
    assert result == {
        "id": 777,
        "ledger_key": inserted_rows[0].ledger_key,
        "duplicate": True,
    }


@pytest.mark.asyncio
async def test_integrity_error_without_matching_key_returns_none(monkeypatch):
    sessions, inserted_rows = _install_ledger_integrity_race_sessions(
        monkeypatch, competitor_exists=False
    )

    result = await strategy_history.record_daily_signal_ledger(
        **_ledger_integrity_race_kwargs()
    )

    assert len(sessions) == 2
    assert len(inserted_rows) == 1
    assert result is None


@pytest.mark.asyncio
async def test_sqlite_unique_collision_recovers_as_duplicate(tmp_path, monkeypatch):
    # File-backed SQLite gives the competing writer and attempted writer
    # independent sessions and exercises the real unique index.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ledger-integrity-race.sqlite'}"
    )
    RealSession = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory_calls = 0

        class EmptyResult:
            def scalar_one_or_none(self):
                return None

        class FirstAttemptSession:
            def __init__(self):
                self.inner = RealSession()
                self.attempted_row = None

            async def __aenter__(self):
                await self.inner.__aenter__()
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return await self.inner.__aexit__(exc_type, exc, traceback)

            async def execute(self, statement):
                # Simulate the initial SELECT observing no matching key.
                # No SQL is issued here, so the competitor can commit first.
                return EmptyResult()

            def add(self, row):
                self.attempted_row = row
                self.inner.add(row)

            async def commit(self):
                pending = self.attempted_row
                assert pending is not None

                # A different session wins the same ledger_key before the
                # attempted insert is committed.
                competitor = DailySignalLedger(
                    timestamp=pending.timestamp,
                    symbol=pending.symbol,
                    action=pending.action,
                    option_type=pending.option_type,
                    strike=pending.strike,
                    expiry=pending.expiry,
                    spot=pending.spot,
                    option_ltp_snapshot=pending.option_ltp_snapshot,
                    entry_price=pending.entry_price,
                    signal_strength=pending.signal_strength,
                    confidence=pending.confidence,
                    lifecycle=pending.lifecycle,
                    confirmations=pending.confirmations,
                    market_snapshot_timestamp=pending.market_snapshot_timestamp,
                    technical_data_source=pending.technical_data_source,
                    confluence=pending.confluence,
                    mtf_freshness=pending.mtf_freshness,
                    entry_snapshot=pending.entry_snapshot,
                    outcome=None,
                    ledger_key=pending.ledger_key,
                )
                async with RealSession() as competitor_session:
                    competitor_session.add(competitor)
                    await competitor_session.commit()

                # Real SQLite must reject the second insert on the unique key.
                await self.inner.commit()

            async def refresh(self, row):
                await self.inner.refresh(row)

        def session_factory():
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls == 1:
                return FirstAttemptSession()
            return RealSession()

        monkeypatch.setattr(
            strategy_history, "AsyncSessionLocal", session_factory
        )

        result = await strategy_history.record_daily_signal_ledger(
            **_ledger_integrity_race_kwargs()
        )

        assert factory_calls == 2, "recovery should open a new DB session"
        assert result is not None
        assert result["duplicate"] is True

        async with RealSession() as db:
            rows = (
                await db.execute(select(DailySignalLedger))
            ).scalars().all()

        assert len(rows) == 1
        assert rows[0].ledger_key == result["ledger_key"]
        assert rows[0].id == result["id"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wait_is_not_written_to_daily_ledger(ledger_session):
    result = await strategy_history.record_daily_signal_ledger(
        symbol="FINNIFTY",
        action="WAIT",
        option_type="CE",
        strike=25000,
        spot=25010,
        lifecycle="WAIT",
    )

    assert result is None

    async with ledger_session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(DailySignalLedger)
            )
        ).scalar_one()

    assert count == 0
