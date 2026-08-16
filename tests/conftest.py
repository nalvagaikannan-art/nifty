"""
Shared pytest fixtures.

`app.utils.cache` keeps its in-memory fallback cache as a plain module-level
dict that persists for the life of the process (see app/utils/cache.py) —
correct for the running app, but it means two tests that call the same
cached method (e.g. `DataFetcher.get_spot("NIFTY")`) with different mocked
`_get` return values would silently see the FIRST test's cached result in
the second test, never actually exercising the second mock. This fixture
resets that dict before and after every test so each test's mock is always
the one actually hit.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_in_memory_cache():
    from app.utils import cache as cache_module
    cache_module._cache.clear()
    yield
    cache_module._cache.clear()
