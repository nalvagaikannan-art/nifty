"""Real PostgreSQL schema and paper-ledger persistence integration test.

GitHub Actions supplies an ephemeral PostgreSQL service. Local runs skip this
integration test unless POSTGRES_TEST_URL is explicitly configured.
"""
import os
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.db_lifecycle import initialize_database_schema
from app.models import DailySignalLedger  # registers the production ledger table


@pytest.mark.asyncio
async def test_postgres_schema_and_paper_ledger_survive_engine_restart():
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is not configured")

    ledger_key = f"qa-postgres-integration-{uuid.uuid4().hex}"
    engine = create_async_engine(database_url, pool_pre_ping=True)

    try:
        # Exercise the same schema initializer used by the application startup.
        await initialize_database_schema(engine, Base.metadata)

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            row = DailySignalLedger(
                symbol="NIFTY",
                action="BUY CE",
                option_type="CE",
                strike=25000.0,
                spot=25000.0,
                signal_strength=60.0,
                confidence=60.0,
                lifecycle="CONFIRMED_CALL",
                confirmations=3,
                ledger_key=ledger_key,
                entry_snapshot={"paper_only": True, "actual_trade": False},
            )
            session.add(row)
            await session.commit()

        # Dispose all pooled connections and create a fresh engine to model
        # an application process restart while keeping the PostgreSQL service.
        await engine.dispose()
        engine = create_async_engine(database_url, pool_pre_ping=True)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async with sessions() as session:
            persisted = await session.scalar(
                select(DailySignalLedger).where(
                    DailySignalLedger.ledger_key == ledger_key
                )
            )
            assert persisted is not None
            assert persisted.symbol == "NIFTY"
            assert persisted.action == "BUY CE"
            assert persisted.entry_snapshot == {
                "paper_only": True,
                "actual_trade": False,
            }

            await session.execute(
                delete(DailySignalLedger).where(
                    DailySignalLedger.ledger_key == ledger_key
                )
            )
            await session.commit()
    finally:
        await engine.dispose()
