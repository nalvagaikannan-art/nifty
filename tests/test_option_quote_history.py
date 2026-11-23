import asyncio
import time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import OptionQuoteHistory
from app.services import data_fetcher, history_service
from app.services.option_snapshot_state import snapshot_persistence_enabled
from app.utils import helpers


@pytest.mark.asyncio
async def test_quote_history_persists_ws_and_rest_prices_without_changing_legacy_table(
    monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(history_service, "AsyncSessionLocal", session_factory)

    now = time.time()
    chain = {
        "symbol": "NIFTY",
        "expiry": "27OCT2026",
        "data_source": "angel_one",
        "data": [{
            "strikePrice": 25000,
            "CE": {
                "lastPrice": 99.0,
                "bidprice": 98.0,
                "askPrice": 100.0,
                "exchangeFeedTime": "17-Oct-2026 10:00:00",
                "exchangeTradeTime": "17-Oct-2026 09:59:59",
                "live_ws_quote": {
                    "source": "angel_one_websocket",
                    "received_at": now,
                    "ltp": 100.5,
                    "bid": 100.0,
                    "ask": 101.0,
                    "exchange_timestamp": "2026-10-17T15:30:00+05:30",
                    "exchange_timestamp_ms": 1792231200000,
                },
            },
            "PE": {
                "lastPrice": 95.0,
                "bidprice": 94.0,
                "askPrice": 96.0,
                "exchangeFeedTime": "17-Oct-2026 10:00:00",
                "exchangeTradeTime": "17-Oct-2026 09:59:58",
            },
        }],
    }

    try:
        assert await history_service.save_option_quote_snapshot(chain, "NIFTY")

        async with session_factory() as session:
            result = await session.execute(
                select(OptionQuoteHistory).order_by(OptionQuoteHistory.option_type)
            )
            rows = result.scalars().all()

        assert len(rows) == 2
        ce = next(row for row in rows if row.option_type == "CE")
        pe = next(row for row in rows if row.option_type == "PE")

        assert ce.quote_source == "angel_one_websocket"
        assert ce.last_price == 100.5
        assert ce.bid == 100.0
        assert ce.ask == 101.0
        assert ce.ws_received_at == now
        assert ce.ws_exchange_timestamp == "2026-10-17T15:30:00+05:30"
        assert ce.ws_exchange_timestamp_ms == "1792231200000"
        assert ce.exchange_trade_time == "17-Oct-2026 09:59:59"
        assert ce.recorded_at is not None

        assert pe.quote_source == "angel_one_rest"
        assert pe.last_price == 95.0
        assert pe.bid == 94.0
        assert pe.ask == 96.0
        assert pe.ws_received_at is None
        assert pe.exchange_feed_time == "17-Oct-2026 10:00:00"
    finally:
        await engine.dispose()


def test_quote_history_persistence_has_independent_60_second_throttle(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(data_fetcher, "_OPTION_QUOTE_HISTORY_PERSIST_LAST_SAVED", {})
    monkeypatch.setattr(data_fetcher, "_OPTION_QUOTE_HISTORY_PERSIST_LOCK", asyncio.Lock())
    token = snapshot_persistence_enabled.set(True)
    calls = []

    async def fake_save(chain, symbol):
        calls.append((symbol, chain["expiry"]))
        return True

    chain = {
        "symbol": "FINNIFTY",
        "expiry": "27OCT2026",
        "data": [{"strikePrice": 25000}],
    }

    try:
        with patch.object(helpers, "is_market_hours_ist", return_value=True), \
             patch.object(history_service, "save_option_quote_snapshot", new=fake_save):
            first = asyncio.run(
                data_fetcher._persist_option_quote_history_if_due(
                    chain, "FINNIFTY", now_mono=1000.0
                )
            )
            too_soon = asyncio.run(
                data_fetcher._persist_option_quote_history_if_due(
                    chain, "FINNIFTY", now_mono=1059.9
                )
            )
            due_again = asyncio.run(
                data_fetcher._persist_option_quote_history_if_due(
                    chain, "FINNIFTY", now_mono=1060.0
                )
            )

        assert first is True
        assert too_soon is False
        assert due_again is True
        assert calls == [
            ("FINNIFTY", "27OCT2026"),
            ("FINNIFTY", "27OCT2026"),
        ]
    finally:
        snapshot_persistence_enabled.reset(token)


@pytest.mark.asyncio
async def test_get_option_chain_persists_legacy_then_merges_then_saves_quote_history(
    monkeypatch,
):
    fetcher = data_fetcher.DataFetcher()
    base_chain = {
        "symbol": "NIFTY",
        "expiry": "27OCT2026",
        "data_source": "angel_one",
        "data": [],
    }
    merged_chain = {
        **base_chain,
        "live_option_ticks_merged": 1,
    }
    events = []

    async def fake_cached(symbol, expiry=None, strikes_each_side=10):
        events.append("fetch")
        assert symbol == "NIFTY"
        return base_chain

    async def fake_legacy_persist(chain, fallback_symbol):
        assert chain is base_chain
        assert fallback_symbol == "NIFTY"
        events.append("legacy_persist")
        return True

    def fake_merge(chain):
        assert chain is base_chain
        events.append("merge")
        return merged_chain

    async def fake_quote_history(chain, fallback_symbol):
        assert chain is merged_chain
        assert fallback_symbol == "NIFTY"
        events.append("quote_history")
        return True

    monkeypatch.setattr(fetcher, "_get_option_chain_cached", fake_cached)
    monkeypatch.setattr(
        data_fetcher, "_persist_option_chain_snapshot_if_due",
        fake_legacy_persist,
    )
    monkeypatch.setattr(fetcher, "_merge_live_option_ticks", fake_merge)
    monkeypatch.setattr(
        data_fetcher, "_persist_option_quote_history_if_due",
        fake_quote_history,
    )

    result = await fetcher.get_option_chain("NIFTY")

    assert result is merged_chain
    assert events == [
        "fetch",
        "legacy_persist",
        "merge",
        "quote_history",
    ]

# OPTION_PRICE_HISTORY_READER_REGRESSION_20261022
from datetime import datetime as _reader_datetime


async def _option_price_reader_test_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(history_service, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        history_service, "now_utc_naive",
        lambda: _reader_datetime(2026, 10, 22, 5, 0),
    )
    return engine, session_factory


def _option_price_reader_row(stamp, strike, side, price, source):
    return OptionQuoteHistory(
        recorded_at=stamp,
        symbol="NIFTY",
        expiry="27OCT2026",
        strike=strike,
        option_type=side,
        last_price=price,
        bid=price - 0.5,
        ask=price + 0.5,
        quote_source=source,
    )


def _option_price_reader_current_chain(ce_price=110.0, pe_price=80.0):
    def live_leg(price):
        return {
            "lastPrice": price - 1.0,
            "bidprice": price - 0.5,
            "askPrice": price + 0.5,
            "live_ws_quote": {
                "source": "angel_one_websocket",
                "received_at": time.time(),
                "age_sec": 1.0,
                "ltp": price,
                "bid": price - 0.5,
                "ask": price + 0.5,
                "exchange_timestamp": "2026-10-22T10:29:59+05:30",
            },
        }

    return {
        "symbol": "NIFTY",
        "expiry": "27OCT2026",
        "data_source": "angel_one",
        "data": [{
            "strikePrice": 25000.0,
            "CE": live_leg(ce_price),
            "PE": live_leg(pe_price),
        }],
    }


@pytest.mark.asyncio
async def test_option_price_reader_uses_eligible_snapshot_and_same_source(monkeypatch):
    engine, session_factory = await _option_price_reader_test_db(monkeypatch)
    old_stamp = _reader_datetime(2026, 10, 22, 4, 30)
    too_recent_stamp = _reader_datetime(2026, 10, 22, 4, 50)

    try:
        async with session_factory() as session:
            session.add_all([
                _option_price_reader_row(
                    old_stamp, 25000.0, "CE", 100.0, "angel_one_websocket"
                ),
                _option_price_reader_row(
                    old_stamp, 25000.0, "PE", 90.0, "angel_one_websocket"
                ),
                # These are newer than the 15-minute cutoff and must not win.
                _option_price_reader_row(
                    too_recent_stamp, 25000.0, "CE", 108.0, "angel_one_websocket"
                ),
                _option_price_reader_row(
                    too_recent_stamp, 25000.0, "PE", 84.0, "angel_one_websocket"
                ),
            ])
            await session.commit()

        result = await history_service.get_option_price_change_since(
            "NIFTY", "27OCT2026",
            _option_price_reader_current_chain(),
            minutes_ago=15,
        )

        assert result["available"] is True
        assert result["window_minutes"] == 30.0
        assert result["reference_recorded_at"] == old_stamp.isoformat()
        assert result["matched_contracts"] == 2
        by_side = {item["option_type"]: item for item in result["price_changes"]}
        assert by_side["CE"]["previous_price"] == 100.0
        assert by_side["CE"]["current_price"] == 110.0
        assert by_side["CE"]["price_change"] == 10.0
        assert by_side["PE"]["previous_price"] == 90.0
        assert by_side["PE"]["current_price"] == 80.0
        assert by_side["PE"]["price_change"] == -10.0
        assert all(
            item["quote_source"] == "angel_one_websocket"
            for item in result["price_changes"]
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_option_price_reader_excludes_source_mismatch(monkeypatch):
    engine, session_factory = await _option_price_reader_test_db(monkeypatch)
    old_stamp = _reader_datetime(2026, 10, 22, 4, 30)

    try:
        async with session_factory() as session:
            session.add_all([
                # Current CE quote is WS, so REST history must not be compared.
                _option_price_reader_row(
                    old_stamp, 25000.0, "CE", 100.0, "angel_one_rest"
                ),
                _option_price_reader_row(
                    old_stamp, 25000.0, "PE", 90.0, "angel_one_websocket"
                ),
            ])
            await session.commit()

        result = await history_service.get_option_price_change_since(
            "NIFTY", "27OCT2026",
            _option_price_reader_current_chain(),
            minutes_ago=15,
        )

        assert result["available"] is True
        assert result["matched_contracts"] == 1
        assert result["source_mismatch_count"] == 1
        assert len(result["price_changes"]) == 1
        assert result["price_changes"][0]["option_type"] == "PE"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_option_price_reader_returns_unavailable_without_eligible_history(monkeypatch):
    engine, session_factory = await _option_price_reader_test_db(monkeypatch)
    too_recent_stamp = _reader_datetime(2026, 10, 22, 4, 55)

    try:
        async with session_factory() as session:
            session.add(
                _option_price_reader_row(
                    too_recent_stamp, 25000.0, "CE", 100.0,
                    "angel_one_websocket",
                )
            )
            await session.commit()

        result = await history_service.get_option_price_change_since(
            "NIFTY", "27OCT2026",
            _option_price_reader_current_chain(),
            minutes_ago=15,
        )

        assert result["available"] is False
        assert result["status"] == "unavailable"
        assert "no same-session quote snapshot" in result["reason"]
    finally:
        await engine.dispose()
