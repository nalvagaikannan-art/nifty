import pytest

from app.database import _normalize_async_database_url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "postgres://user:password@db.example:5432/nifty",
            "postgresql+asyncpg://user:password@db.example:5432/nifty",
        ),
        (
            "postgresql://user:password@db.example:5432/nifty?sslmode=require",
            "postgresql+asyncpg://user:password@db.example:5432/nifty?sslmode=require",
        ),
    ],
)
def test_plain_postgres_urls_are_normalized_for_async_engine(source, expected):
    assert _normalize_async_database_url(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "postgresql+asyncpg://user:password@db.example:5432/nifty",
        "sqlite+aiosqlite:///./data/analyzer_pro.db",
    ],
)
def test_non_plain_postgres_urls_are_left_unchanged(source):
    assert _normalize_async_database_url(source) == source
