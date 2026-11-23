import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.services.strategy_history as strategy_history
from app.database import Base
from app.models import DailySignalLedger, SignalHistory


@pytest.fixture
async def history_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(strategy_history, "AsyncSessionLocal", Session)
    yield Session
    await engine.dispose()


def closed_wait_kwargs(**overrides):
    values = {
        "symbol": "NIFTY",
        "strategy": "WAIT",
        "score": 50,
        "market_state": "MARKET CLOSED",
        "confidence": 30,
        "spot": 22520.45,
        "pcr": 0,
        "vix": 0,
        "reasons": ["Market closed ? live intraday technical data unavailable"],
    }
    values.update(overrides)
    return values


async def row_count(Session):
    async with Session() as db:
        result = await db.execute(
            select(func.count()).select_from(SignalHistory)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_identical_consecutive_closed_wait_is_not_written_twice(history_session):
    kwargs = closed_wait_kwargs()

    first = await strategy_history.record_signal_persistent(**kwargs)
    second = await strategy_history.record_signal_persistent(**kwargs)

    assert first["strategy"] == "WAIT"
    assert second["strategy"] == "WAIT"
    assert second["market_state"] == "MARKET CLOSED"
    assert await row_count(history_session) == 1


@pytest.mark.asyncio
async def test_changed_closed_wait_snapshot_is_written(history_session):
    await strategy_history.record_signal_persistent(**closed_wait_kwargs())
    await strategy_history.record_signal_persistent(
        **closed_wait_kwargs(spot=22521.0)
    )

    assert await row_count(history_session) == 2


@pytest.mark.asyncio
async def test_market_open_wait_is_not_suppressed(history_session):
    kwargs = closed_wait_kwargs(market_state="MARKET OPEN")

    await strategy_history.record_signal_persistent(**kwargs)
    await strategy_history.record_signal_persistent(**kwargs)

    assert await row_count(history_session) == 2
