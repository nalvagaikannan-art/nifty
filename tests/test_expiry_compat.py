from sqlalchemy import Column, MetaData, String, Table, create_engine, select

from app.utils.expiry import (
    canonical_expiry,
    expiry_matches,
    expiry_sql_key,
    expiry_storage_variants,
)


def test_canonical_expiry_unifies_observed_formats():
    assert canonical_expiry("27-Oct-2026") == "27OCT2026"
    assert canonical_expiry("27OCT2026") == "27OCT2026"
    assert canonical_expiry("2026-10-27") == "27OCT2026"


def test_variants_include_observed_legacy_spellings():
    variants = set(expiry_storage_variants("27OCT2026"))
    assert "27OCT2026" in variants
    assert "27-Oct-2026" in variants
    assert "2026-10-27" in variants


def test_expiry_read_query_matches_old_and_new_stored_formats():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    table = Table(
        "expiry_compat_test",
        metadata,
        Column("expiry", String, nullable=False),
    )
    metadata.create_all(engine)
    stored = ["27OCT2026", "27-Oct-2026", "2026-10-27"]

    with engine.begin() as conn:
        conn.execute(table.insert(), [{"expiry": v} for v in stored])

    with engine.connect() as conn:
        matched = conn.execute(
            select(table.c.expiry).where(
                expiry_matches(table.c.expiry, "27OCT2026")
            )
        ).scalars().all()

        compact = conn.execute(
            select(table.c.expiry).where(
                expiry_sql_key(table.c.expiry) == canonical_expiry("27-Oct-2026")
            )
        ).scalars().all()

    assert set(matched) == set(stored)
    assert set(compact) == {"27OCT2026", "27-Oct-2026"}


def test_persistence_helper_passes_canonical_expiry_to_writer():
    import asyncio
    from unittest.mock import patch

    from app.services import data_fetcher, history_service
    from app.services.option_snapshot_state import snapshot_persistence_enabled
    import app.utils.helpers as helpers

    captured = {}

    async def fake_save(symbol, expiry, option_df):
        captured.update(symbol=symbol, expiry=expiry, rows=len(option_df))
        return True

    async def scenario():
        old_last = data_fetcher._OPTION_SNAPSHOT_PERSIST_LAST_SAVED
        data_fetcher._OPTION_SNAPSHOT_PERSIST_LAST_SAVED = {}
        token = snapshot_persistence_enabled.set(True)
        chain = {
            "symbol": "FINNIFTY",
            "expiry": "27-Oct-2026",
            "data": [{
                "strikePrice": 25000,
                "CE": {
                    "lastPrice": 100, "bidprice": 99, "askPrice": 101,
                    "totalTradedVolume": 10, "openInterest": 100,
                    "impliedVolatility": 18,
                },
                "PE": {
                    "lastPrice": 95, "bidprice": 94, "askPrice": 96,
                    "totalTradedVolume": 12, "openInterest": 110,
                    "impliedVolatility": 19,
                },
            }],
        }
        try:
            with patch.object(helpers, "is_market_hours_ist", return_value=True), \
                 patch.object(history_service, "save_option_chain_snapshot", new=fake_save):
                saved = await data_fetcher._persist_option_chain_snapshot_if_due(
                    chain, "FINNIFTY", now_mono=2000.0
                )
            assert saved is True
            assert captured == {
                "symbol": "FINNIFTY", "expiry": "27OCT2026", "rows": 1
            }
        finally:
            snapshot_persistence_enabled.reset(token)
            data_fetcher._OPTION_SNAPSHOT_PERSIST_LAST_SAVED = old_last

    asyncio.run(scenario())


def test_angel_option_expiries_use_ist_calendar_date_before_utc_midnight(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.services import angel_one

    # 01:00 IST on Nov 23 is still Nov 22 in UTC. The Nov 23 contract
    # must remain valid because Indian exchange expiry dates use IST.
    monkeypatch.setattr(
        angel_one,
        "now_ist",
        lambda: datetime(2026, 11, 23, 1, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert angel_one._valid_option_expiries([
        "20NOV2026", "23NOV2026", "24NOV2026", "INVALID"
    ]) == ["23NOV2026", "24NOV2026"]


def test_angel_option_expiries_roll_forward_after_ist_expiry_date(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.services import angel_one

    monkeypatch.setattr(
        angel_one,
        "now_ist",
        lambda: datetime(2026, 11, 24, 0, 5, tzinfo=ZoneInfo("Asia/Kolkata")),
    )

    assert angel_one._valid_option_expiries([
        "23NOV2026", "24NOV2026", "26NOV2026"
    ]) == ["24NOV2026", "26NOV2026"]
