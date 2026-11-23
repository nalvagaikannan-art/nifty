"""Database startup schema initialization shared by the application lifespan."""


async def initialize_database_schema(engine, metadata):
    """Create missing tables and apply SQLite-only connection pragmas.

    Database errors intentionally propagate so the service cannot report a
    successful startup while its persistence layer is unavailable.
    """
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        if engine.url.drivername == "sqlite+aiosqlite":
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
