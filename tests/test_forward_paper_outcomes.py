from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    DailySignalLedger,
    ForwardPaperOutcome,
    MarketData,
    OptionQuoteHistory,
)
from app.services import forward_paper_outcomes as outcomes


@pytest.fixture
async def outcome_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(outcomes, "AsyncSessionLocal", factory)
    yield factory
    await engine.dispose()


def _entry_row(symbol, action, lifecycle, event_ts, eligible=True):
    from app.services.entry_risk_authority import build_entry_risk_authority

    lot_size = {
        "NIFTY": 65,
        "BANKNIFTY": 30,
        "FINNIFTY": 60,
        "SENSEX": 20,
    }[symbol]
    option_type = "CE" if action.endswith("CE") else "PE"
    decision_action = action if eligible else "WAIT"
    authority = None

    if eligible and action in ("BUY CE", "BUY PE"):
        stop = 80.0
        risk_per_lot = (100.0 - stop) * lot_size
        authority = build_entry_risk_authority(
            action=action,
            risk_assessment={
                "allowed": True,
                "lots": 1,
                "quantity": lot_size,
                "max_loss": risk_per_lot,
                "max_risk_amount": risk_per_lot,
                "risk_per_lot": risk_per_lot,
                "no_trade_reasons": [],
                "reasons": ["Test approval"],
                "warnings": [],
            },
            selected_contract={
                "symbol": symbol,
                "expiry": "29OCT2026",
                "strike": 25000.0,
                "type": option_type,
                "entry_price": 100.0,
                "sl": stop,
            },
            trade_levels={
                "option_entry": 100.0,
                "option_sl": stop,
                "option_rr_ratio": 2.0,
            },
            lot_size=lot_size,
        )

    capture = {
        "schema_version": 2,
        "eligible": eligible,
        "actual_trade": False,
        "entry_risk_authority": authority,
        "contract": {
            "expiry": "29OCT2026",
            "strike": 25000.0,
            "option_type": option_type,
            "lot_size": lot_size,
        },
        "quote": {
            "ltp": 100.0,
            "entry_price": 100.0,
            "bid": 99.0,
            "ask": 101.0,
            "price_source": "angel_one_websocket",
        },
        "spot": {
            "price": 20000.0,
            "data_source": "test_fixture",
        },
    }
    decision = {
        "metadata_version": 2,
        "fresh_entry_allowed": eligible,
        "entry_risk_authority": authority,
        "lifecycle": lifecycle,
        "signal_action": decision_action,
        "entry_gate": {"allowed": eligible},
    }
    snapshot = {
        "entry_price": 100.0,
        "sl": 80.0,
        "ltp": 100.0,
        "bid": 99.0,
        "ask": 101.0,
        "price_source": "angel_one_websocket",
        "lot_size": lot_size,
        "forward_paper_capture": capture,
        "strategy_decision": decision,
    }
    return DailySignalLedger(
        timestamp=event_ts,
        symbol=symbol,
        action=action,
        option_type=option_type,
        strike=25000.0,
        expiry="29OCT2026",
        spot=20000.0,
        option_ltp_snapshot=100.0,
        entry_price=100.0,
        signal_strength=70.0,
        confidence=60.0,
        lifecycle=lifecycle,
        confirmations=3,
        market_snapshot_timestamp=event_ts.isoformat(),
        technical_data_source="test",
        confluence={},
        mtf_freshness={},
        entry_snapshot=snapshot,
        outcome=None,
        ledger_key=f"{symbol}|{action}|{event_ts.isoformat()}",
    )



def _quote(symbol, timestamp, ltp, option_type="CE", source="angel_one_websocket"):
    return OptionQuoteHistory(
        recorded_at=timestamp,
        symbol=symbol,
        expiry="29OCT2026",
        strike=25000.0,
        option_type=option_type,
        last_price=ltp,
        bid=ltp - 1.0,
        ask=ltp + 1.0,
        quote_source=source,
        ws_received_at=timestamp.replace(tzinfo=timezone.utc).timestamp()
        if source == "angel_one_websocket" else None,
        ws_exchange_timestamp=timestamp.replace(tzinfo=timezone.utc).isoformat()
        if source == "angel_one_websocket" else None,
        ws_exchange_timestamp_ms=None,
    )


@pytest.mark.asyncio
async def test_all_four_symbols_persist_independent_horizons_idempotently(outcome_session):
    event = datetime(2026, 10, 27, 4, 0)  # 09:30 IST, UTC-naive DB timestamp
    symbol_cases = [
        ("NIFTY", "BUY CE", "CONFIRMED_CALL", 1.0, 1.0),
        ("BANKNIFTY", "BUY CE", "CONFIRMED_CALL", 1.0, 1.0),
        ("FINNIFTY", "BUY PE", "CONFIRMED_PUT", -1.0, 1.0),
        ("SENSEX", "BUY PE", "CONFIRMED_PUT", -1.0, 1.0),
    ]

    async with outcome_session() as session:
        for symbol, action, lifecycle, spot_sign, option_sign in symbol_cases:
            session.add(_entry_row(symbol, action, lifecycle, event))
            # Include a slightly adverse observation before the 5-minute target.
            adverse_ltp = 99.5 if action.startswith("BUY") else 100.5
            session.add(_quote(symbol, event + timedelta(minutes=1), adverse_ltp, option_type=("CE" if action.endswith("CE") else "PE")))

            for horizon in outcomes.OUTCOME_HORIZONS_MINUTES:
                target = event + timedelta(minutes=horizon)
                spot_price = 20000.0 + spot_sign * horizon
                option_ltp = 100.0 + option_sign * horizon
                session.add(
                    MarketData(
                        symbol=symbol,
                        price=spot_price,
                        timestamp=target,
                    )
                )
                session.add(_quote(symbol, target, option_ltp, option_type=("CE" if action.endswith("CE") else "PE")))
        await session.commit()

    result = await outcomes.process_forward_paper_outcomes(
        now=event + timedelta(minutes=70)
    )
    assert result["eligible_entries"] == 4
    assert result["outcome_rows_created"] == 20
    assert result["matured_rows"] == 20

    async with outcome_session() as session:
        rows = (
            await session.execute(
                select(ForwardPaperOutcome).order_by(
                    ForwardPaperOutcome.symbol,
                    ForwardPaperOutcome.horizon_minutes,
                )
            )
        ).scalars().all()

    assert len(rows) == 20
    assert {row.symbol for row in rows} == {
        "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"
    }
    for symbol, *_ in symbol_cases:
        selected = [row for row in rows if row.symbol == symbol]
        assert [row.horizon_minutes for row in selected] == [5, 10, 15, 30, 60]
        assert all(row.status == "matured" for row in selected)
        assert all(row.spot_directional_return_pct > 0 for row in selected)
        assert all(row.premium_action_adjusted_return_pct > 0 for row in selected)
        assert all(row.execution_aware_return_pct is not None for row in selected)
        assert all(row.mfe_pct is not None for row in selected)
        assert all(row.mae_pct is not None for row in selected)
        assert all(row.option_quote_source == "angel_one_websocket" for row in selected)
        assert all(row.details["actual_trade"] is False for row in selected)

    repeated = await outcomes.process_forward_paper_outcomes(
        now=event + timedelta(minutes=70)
    )
    assert repeated["outcome_rows_created"] == 0
    assert repeated["outcome_rows_updated"] == 0
    assert repeated["terminal_rows_skipped"] == 20


@pytest.mark.asyncio
async def test_source_mismatch_is_not_used_for_option_outcome(outcome_session):
    event = datetime(2026, 10, 27, 4, 0)
    async with outcome_session() as session:
        session.add(_entry_row("NIFTY", "BUY CE", "CONFIRMED_CALL", event))
        session.add(
            MarketData(
                symbol="NIFTY",
                price=20005.0,
                timestamp=event + timedelta(minutes=5),
            )
        )
        # Same contract, but not the frozen entry source. Must not be used.
        session.add(
            _quote(
                "NIFTY",
                event + timedelta(minutes=5),
                125.0,
                source="angel_one_rest",
            )
        )
        await session.commit()

    result = await outcomes.process_forward_paper_outcomes(
        now=event + timedelta(minutes=15)
    )
    assert result["eligible_entries"] == 1

    async with outcome_session() as session:
        row = (
            await session.execute(
                select(ForwardPaperOutcome).where(
                    ForwardPaperOutcome.horizon_minutes == 5
                )
            )
        ).scalar_one()

    assert row.status == "matured_partial"
    assert row.spot_return_pct is not None
    assert row.option_target_ltp is None
    assert row.premium_action_adjusted_return_pct is None
    assert row.details["option_status"] == "no_matching_source_quote"


@pytest.mark.asyncio
async def test_out_of_session_horizon_is_not_graded(outcome_session):
    event = datetime(2026, 10, 27, 9, 30)  # 15:00 IST
    async with outcome_session() as session:
        session.add(_entry_row("SENSEX", "BUY PE", "CONFIRMED_PUT", event))
        await session.commit()

    result = await outcomes.process_forward_paper_outcomes(
        now=event + timedelta(minutes=40)
    )
    assert result["eligible_entries"] == 1

    async with outcome_session() as session:
        row = (
            await session.execute(
                select(ForwardPaperOutcome).where(
                    ForwardPaperOutcome.symbol == "SENSEX",
                    ForwardPaperOutcome.horizon_minutes == 60,
                )
            )
        ).scalar_one()

    assert row.status == "session_unavailable"
    assert row.details["reason"] == "target_outside_same_nse_session"


@pytest.mark.asyncio
async def test_ineligible_row_does_not_create_outcome_records(outcome_session):
    event = datetime(2026, 10, 27, 4, 0)
    async with outcome_session() as session:
        session.add(
            _entry_row(
                "NIFTY", "BUY CE", "CONFIRMED_CALL", event, eligible=False
            )
        )
        await session.commit()

    result = await outcomes.process_forward_paper_outcomes(
        now=event + timedelta(minutes=70)
    )
    assert result["eligible_entries"] == 0
    assert result["outcome_rows_created"] == 0

    async with outcome_session() as session:
        count = await session.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(ForwardPaperOutcome)
        )
    assert count == 0


@pytest.mark.asyncio
async def test_legacy_v1_entry_does_not_create_new_outcome_rows(outcome_session):
    event = datetime(2026, 10, 27, 4, 0)
    entry = _entry_row("NIFTY", "BUY CE", "CONFIRMED_CALL", event)
    capture = entry.entry_snapshot["forward_paper_capture"]
    decision = entry.entry_snapshot["strategy_decision"]
    capture["schema_version"] = 1
    capture.pop("entry_risk_authority", None)
    decision["metadata_version"] = 1
    decision.pop("entry_risk_authority", None)

    async with outcome_session() as session:
        session.add(entry)
        await session.commit()

    result = await outcomes.process_forward_paper_outcomes(
        now=event + timedelta(minutes=70)
    )
    assert result["eligible_entries"] == 0
    assert result["legacy_pending_entries"] == 0
    assert result["outcome_rows_created"] == 0

    async with outcome_session() as session:
        count = await session.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(ForwardPaperOutcome)
        )
    assert count == 0


@pytest.mark.asyncio
async def test_existing_legacy_v1_pending_outcome_continues_grading(outcome_session):
    event = datetime(2026, 10, 27, 4, 0)
    entry = _entry_row("NIFTY", "BUY CE", "CONFIRMED_CALL", event)
    capture = entry.entry_snapshot["forward_paper_capture"]
    decision = entry.entry_snapshot["strategy_decision"]
    capture["schema_version"] = 1
    capture.pop("entry_risk_authority", None)
    decision["metadata_version"] = 1
    decision.pop("entry_risk_authority", None)

    async with outcome_session() as session:
        session.add(entry)
        await session.flush()
        session.add(
            ForwardPaperOutcome(
                ledger_id=entry.id,
                symbol="NIFTY",
                horizon_minutes=5,
                event_timestamp=event,
                target_timestamp=event + timedelta(minutes=5),
                status="pending",
                status_reason="Legacy pending row",
                details={
                    "schema_version": 1,
                    "actual_trade": False,
                    "state": "pending",
                },
            )
        )
        session.add(
            MarketData(
                symbol="NIFTY",
                price=20005.0,
                timestamp=event + timedelta(minutes=5),
            )
        )
        session.add(
            _quote(
                "NIFTY",
                event + timedelta(minutes=5),
                125.0,
                source="angel_one_rest",
            )
        )
        await session.commit()

    result = await outcomes.process_forward_paper_outcomes(
        now=event + timedelta(minutes=20)
    )
    assert result["eligible_entries"] == 0
    assert result["legacy_pending_entries"] == 1
    assert result["outcome_rows_created"] == 0
    assert result["outcome_rows_updated"] == 1

    async with outcome_session() as session:
        row = (
            await session.execute(
                select(ForwardPaperOutcome).where(
                    ForwardPaperOutcome.horizon_minutes == 5
                )
            )
        ).scalar_one()
    assert row.status == "matured_partial"

@pytest.mark.asyncio
async def test_outcome_scheduler_runs_even_if_market_closed(monkeypatch):
    import asyncio
    from app.services import forward_paper_outcomes as outcome_service
    from app.services import history_collector

    processor_calls = []
    sleep_calls = []

    async def fake_process_forward_paper_outcomes():
        processor_calls.append(True)
        return {
            "ledger_rows_seen": 0,
            "eligible_entries": 0,
            "outcome_rows_created": 0,
            "outcome_rows_updated": 0,
            "pending_rows": 0,
            "matured_rows": 0,
            "session_unavailable_rows": 0,
        }

    async def controlled_sleep(seconds):
        sleep_calls.append(seconds)
        # Allow startup delay and one complete grading iteration, then stop.
        if len(sleep_calls) == 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        outcome_service,
        "process_forward_paper_outcomes",
        fake_process_forward_paper_outcomes,
    )
    monkeypatch.setattr(history_collector.asyncio, "sleep", controlled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await history_collector.run_periodic_forward_paper_outcomes()

    assert processor_calls == [True]
    assert sleep_calls[0] == history_collector.PAPER_OUTCOME_GRADING_START_DELAY_SEC
    assert sleep_calls[1] == pytest.approx(
        history_collector.PAPER_OUTCOME_GRADING_INTERVAL_SEC,
        abs=0.01,
    )

