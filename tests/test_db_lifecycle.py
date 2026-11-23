import pytest

from app.db_lifecycle import initialize_database_schema


class _FakeConnection:
    def __init__(self):
        self.schema_callbacks = []
        self.statements = []

    async def run_sync(self, callback):
        self.schema_callbacks.append(callback)
        callback(object())

    async def exec_driver_sql(self, statement):
        self.statements.append(statement)


class _FakeBegin:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, drivername):
        self.url = type("URL", (), {"drivername": drivername})()
        self.connection = _FakeConnection()

    def begin(self):
        return _FakeBegin(self.connection)


class _FakeMetadata:
    def __init__(self):
        self.create_all_calls = 0

    def create_all(self, connection):
        self.create_all_calls += 1


@pytest.mark.asyncio
async def test_schema_is_created_once_for_postgresql_without_sqlite_pragmas():
    engine = _FakeEngine("postgresql+asyncpg")
    metadata = _FakeMetadata()

    await initialize_database_schema(engine, metadata)

    assert metadata.create_all_calls == 1
    assert len(engine.connection.schema_callbacks) == 1
    assert engine.connection.statements == []


@pytest.mark.asyncio
async def test_sqlite_schema_initialization_applies_wal_pragmas():
    engine = _FakeEngine("sqlite+aiosqlite")
    metadata = _FakeMetadata()

    await initialize_database_schema(engine, metadata)

    assert len(engine.connection.schema_callbacks) == 1
    assert engine.connection.statements == [
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
    ]


class _FailingConnection(_FakeConnection):
    async def run_sync(self, callback):
        raise RuntimeError("database unavailable")


class _FailingEngine(_FakeEngine):
    def __init__(self):
        super().__init__("postgresql+asyncpg")
        self.connection = _FailingConnection()


@pytest.mark.asyncio
async def test_database_initialization_error_propagates_and_blocks_startup():
    engine = _FailingEngine()
    metadata = _FakeMetadata()

    with pytest.raises(RuntimeError, match="database unavailable"):
        await initialize_database_schema(engine, metadata)
