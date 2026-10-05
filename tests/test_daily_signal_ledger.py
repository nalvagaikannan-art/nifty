
import pytest
from sqlalchemy import func, select
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
        entry_snapshot={"strike": 22600, "ltp": 142.5, "entry_price": 143.2},
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
