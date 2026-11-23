"""Real PostgreSQL schema and paper-ledger persistence integration test.

GitHub Actions supplies an ephemeral PostgreSQL service. Local runs skip this
integration test unless POSTGRES_TEST_URL is explicitly configured.
"""
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url
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

        # Exercise a real custom-format backup and restore into a brand-new
        # database. Credentials are passed through PGPASSWORD, never argv.
        pg_dump = shutil.which("pg_dump")
        pg_restore = shutil.which("pg_restore")
        assert pg_dump and pg_restore, "PostgreSQL client tools are required"

        parsed_url = make_url(database_url)
        restored_database = f"nifty_restore_{uuid.uuid4().hex[:16]}"
        admin = await asyncpg.connect(
            user=parsed_url.username,
            password=parsed_url.password,
            host=parsed_url.host,
            port=parsed_url.port or 5432,
            database="postgres",
        )
        restore_engine = None
        try:
            await admin.execute(f'CREATE DATABASE "{restored_database}"')
            with tempfile.TemporaryDirectory(prefix="nifty-pg-backup-") as tmp:
                backup_path = Path(tmp) / "paper-ledger.dump"
                env = os.environ.copy()
                if parsed_url.password:
                    env["PGPASSWORD"] = parsed_url.password
                common = [
                    "--host", parsed_url.host or "127.0.0.1",
                    "--port", str(parsed_url.port or 5432),
                    "--username", parsed_url.username or "",
                    "--no-password",
                ]
                subprocess.run(
                    [
                        pg_dump, *common, "--format=custom",
                        "--no-owner", "--no-privileges",
                        "--dbname", parsed_url.database,
                        "--file", str(backup_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                assert backup_path.is_file() and backup_path.stat().st_size > 0

                subprocess.run(
                    [
                        pg_restore, *common, "--exit-on-error",
                        "--no-owner", "--no-privileges",
                        "--dbname", restored_database, str(backup_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                )

            restored_url = parsed_url.set(database=restored_database)
            restore_engine = create_async_engine(
                restored_url, pool_pre_ping=True
            )
            restore_sessions = async_sessionmaker(
                restore_engine, expire_on_commit=False
            )
            async with restore_sessions() as session:
                restored = await session.scalar(
                    select(DailySignalLedger).where(
                        DailySignalLedger.ledger_key == ledger_key
                    )
                )
                assert restored is not None
                assert restored.symbol == "NIFTY"
                assert restored.action == "BUY CE"
                assert restored.entry_snapshot == {
                    "paper_only": True,
                    "actual_trade": False,
                }
        finally:
            if restore_engine is not None:
                await restore_engine.dispose()
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                restored_database,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{restored_database}"')
            await admin.close()

        async with sessions() as session:
            await session.execute(
                delete(DailySignalLedger).where(
                    DailySignalLedger.ledger_key == ledger_key
                )
            )
            await session.commit()
    finally:
        await engine.dispose()
