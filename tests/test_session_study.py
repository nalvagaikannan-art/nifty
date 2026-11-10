import pytest
from datetime import timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.services.session_study as session_study
from app.database import Base
from app.models import DailySignalLedger, MarketData, OptionData


@pytest.fixture
async def study_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(session_study, "AsyncSessionLocal", Session)

    yield Session

    await engine.dispose()


@pytest.mark.asyncio
async def test_session_study_is_read_only_and_captures_1440_evidence(study_session, monkeypatch):
    async with study_session() as db:
        rows = [
            # Previous trading day.
            MarketData(symbol="NIFTY", price=100, timestamp=session_study._to_ist("2026-10-05T09:15:00+05:30").astimezone(timezone.utc).replace(tzinfo=None)),
            MarketData(symbol="NIFTY", price=101, timestamp=session_study._to_ist("2026-10-05T15:25:00+05:30").astimezone(timezone.utc).replace(tzinfo=None)),
            # Study day.
            MarketData(symbol="NIFTY", price=102, timestamp=session_study._to_ist("2026-10-06T09:15:00+05:30").astimezone(timezone.utc).replace(tzinfo=None)),
            MarketData(symbol="NIFTY", price=103, timestamp=session_study._to_ist("2026-10-06T09:30:00+05:30").astimezone(timezone.utc).replace(tzinfo=None)),
            MarketData(symbol="NIFTY", price=100, timestamp=session_study._to_ist("2026-10-06T15:20:00+05:30").astimezone(timezone.utc).replace(tzinfo=None)),
            MarketData(symbol="NIFTY", price=99, timestamp=session_study._to_ist("2026-10-06T15:30:00+05:30").astimezone(timezone.utc).replace(tzinfo=None)),
        ]
        db.add_all(rows)

        ledger = DailySignalLedger(
            timestamp=session_study._to_ist("2026-10-06T14:46:00+05:30").astimezone(timezone.utc).replace(tzinfo=None),
            symbol="NIFTY",
            action="BUY CE",
            option_type="CE",
            strike=22750,
            expiry="06OCT2026",
            spot=102,
            option_ltp_snapshot=10,
            entry_price=10,
            signal_strength=60,
            confidence=60,
            lifecycle="HOLD_CALL",
            confirmations=3,
            market_snapshot_timestamp="2026-10-06T14:46:00+05:30",
            technical_data_source="intraday_5min_ohlc",
            confluence={"score": 8},
            mtf_freshness={"5min": {"fresh": True}},
            entry_snapshot={"lot_size": 65},
            outcome=None,
            ledger_key="session-study-test",
        )
        db.add(ledger)

        db.add(
            OptionData(
                symbol="NIFTY",
                expiry="06OCT2026",
                strike=22750,
                option_type="CE",
                last_price=12,
                timestamp=session_study._to_ist("2026-10-06T15:40:00+05:30").astimezone(timezone.utc).replace(tzinfo=None),
            )
        )

        await db.commit()

    monkeypatch.setattr(
        session_study,
        "now_utc_naive",
        lambda: session_study._to_ist("2026-10-06T16:00:00+05:30").astimezone(timezone.utc).replace(tzinfo=None),
    )

    result = await session_study.compute_session_study("NIFTY", days=1)

    assert result["basis"].startswith("Read-only Session Study")
    assert result["market_days"] == 1
    assert result["opening_profile"]["valid_gap_days"] == 1
    assert result["opening_profile"]["continuation"] == 1
    assert result["late_session"]["actionable_call_count"] == 1
    assert result["late_session"]["days_with_actionable_call"] == 1
    assert result["late_session"]["calls"][0]["distance_from_1440_minutes"] == 6.0
    assert result["late_session"]["calls"][0]["spot_outcome"] in {
        "favourable", "adverse", "flat", "no_data", "pending"
    }
    assert result["late_session"]["calls"][0]["premium_1540_outcome"] in {
        "favourable", "adverse", "flat", "no_data", "pending"
    }

    async with study_session() as db:
        row = (
            await db.get(DailySignalLedger, 1)
        )
        assert row.outcome is None