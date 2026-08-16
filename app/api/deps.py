"""
IMPORTANT: DataFetcher/MarketAnalyzer/AIEngine are created ONCE at app startup
(see app/main.py lifespan) and reused across every request via app.state —
they are NOT created fresh per-request.

Why this matters:
1. DataFetcher holds an NSE session cookie that takes an extra round-trip to
   obtain. Creating (and closing) a new instance per request meant every
   single API call paid that cost again, and multiplied the number of
   requests NSE sees from this app (higher block/rate-limit risk).
2. The @async_cache TTL cache on DataFetcher's methods keys off the bound
   `self` — a fresh instance every request meant the cache key was different
   every time, so the cache never actually hit. Reusing one instance is what
   makes the cache (and therefore the whole point of caching NSE calls) work.
"""
from fastapi import Request
from app.services.data_fetcher import DataFetcher
from app.services.market_analyzer import MarketAnalyzer
from app.services.ai_engine import AIEngine
from app.services.angel_one import AngelOneSession, angel_session


async def get_fetcher(request: Request) -> DataFetcher:
    return request.app.state.data_fetcher


async def get_angel_session() -> AngelOneSession:
    # AngelOneSession is a module-level singleton (see app/services/angel_one.py)
    # rather than app.state, since it already manages its own login/session
    # lifecycle (token refresh, instrument-master cache) independent of the
    # request lifecycle — same reasoning as DataFetcher's cookie reuse above.
    return angel_session


async def get_analyzer(request: Request) -> MarketAnalyzer:
    return request.app.state.market_analyzer


async def get_ai_engine(request: Request) -> AIEngine:
    # AIEngine is cheap to construct (no network I/O in __init__) and reading
    # settings.ai_provider fresh each call means changing AI_PROVIDER via
    # /api/settings (if ever made mutable) takes effect without a restart.
    return AIEngine()
