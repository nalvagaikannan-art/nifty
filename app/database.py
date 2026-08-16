from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import settings
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = settings.database_url
_is_sqlite = not DATABASE_URL or DATABASE_URL.startswith("sqlite")

if not DATABASE_URL:
    db_path = Path(settings.sqlite_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = f"sqlite+aiosqlite:///{db_path}"
elif not _is_sqlite:
    # Render's managed Postgres (and most hosts) hand out a plain
    # postgres:// or postgresql:// URL — SQLAlchemy's default dialect for
    # that scheme is psycopg2, a SYNC driver, which create_async_engine()
    # below rejects outright. Without this normalisation, wiring
    # render.yaml's `fromDatabase` DATABASE_URL straight into this app
    # would fail at startup every time (review #17's exact failure mode,
    # just one layer deeper — the driver was missing there, the DIALECT
    # marker is missing here). Rewrite to the asyncpg dialect transparently
    # so a bare Render/Heroku-style URL just works.
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgres://"):]
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgresql://"):]

engine_kwargs = {"echo": False}
if _is_sqlite:
    # WAL improves reliability for the live dashboard + accuracy writer.
    engine_kwargs["connect_args"] = {"timeout": 30}
else:
    engine_kwargs.update({"pool_size": 10, "max_overflow": 5, "pool_pre_ping": True})

engine = create_async_engine(DATABASE_URL, **engine_kwargs)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
