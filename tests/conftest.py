"""
Shared pytest fixtures.

Tests must NEVER touch the live analyzer.db. The env vars below are set before
any `app` import so app.database builds its engine on a throw-away temp file.
"""
import os
import tempfile

_TEST_DB_DIR = tempfile.mkdtemp(prefix="nifty_test_db_")
os.environ["SQLITE_DB_PATH"] = os.path.join(_TEST_DB_DIR, "test_analyzer.db")
os.environ["DATABASE_URL"] = ""

import pytest


@pytest.fixture(autouse=True)
def _reset_in_memory_cache():
    # app.utils.cache keeps a module-level dict for the life of the process;
    # clear it so every test's mocks are really exercised.
    from app.utils import cache as cache_module
    cache_module._cache.clear()
    yield
    cache_module._cache.clear()
