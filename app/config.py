from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional, List
import os


class Settings(BaseSettings):
    # App
    env: str = Field("development", alias="ENV")

    # AI providers
    gemini_api_key:   Optional[str] = Field(None, alias="GEMINI_API_KEY")
    openai_api_key:   Optional[str] = Field(None, alias="OPENAI_API_KEY")
    deepseek_api_key: Optional[str] = Field(None, alias="DEEPSEEK_API_KEY")
    ai_provider:      str            = Field("gemini", alias="AI_PROVIDER")

    # Angel One SmartAPI
    angel_api_key:     Optional[str] = Field(None, alias="ANGEL_API_KEY")
    angel_client_id:   Optional[str] = Field(None, alias="ANGEL_CLIENT_ID")
    angel_password:    Optional[str] = Field(None, alias="ANGEL_PASSWORD")
    angel_totp_secret: Optional[str] = Field(None, alias="ANGEL_TOTP_SECRET")

    # Zerodha Kite Connect — second broker provider (spec §5: provider
    # abstraction so NSE-only blocking / Angel-only outage doesn't take the
    # whole app down with it). Kite's login flow needs a browser redirect
    # (no TOTP-only headless login like Angel's), so `kite_access_token` is
    # generated once per day via a separate manual/scripted login step and
    # supplied here directly — see app/services/zerodha.py's module
    # docstring for the exact steps. Leave all four unset to skip Zerodha
    # entirely (DataFetcher falls through to Angel One / NSE exactly as
    # before — no behavior change for existing deployments).
    kite_api_key:      Optional[str] = Field(None, alias="KITE_API_KEY")
    kite_api_secret:   Optional[str] = Field(None, alias="KITE_API_SECRET")
    kite_access_token: Optional[str] = Field(None, alias="KITE_ACCESS_TOKEN")

    # Database
    sqlite_db_path: str = Field("./data/analyzer.db", alias="SQLITE_DB_PATH")
    # Set DATABASE_URL to switch to Postgres, e.g.
    #   postgresql+asyncpg://user:pass@host:5432/nifty_analyzer
    # Leave unset to keep using the SQLite path above (default, single-user/
    # local use). SQLite has no encryption-at-rest and one writer at a time —
    # see CODE_REVIEW.md item #11. `asyncpg` must be installed for Postgres
    # (pip install asyncpg) — not bundled by default since most local/single-
    # user deployments don't need it.
    database_url: Optional[str] = Field(None, alias="DATABASE_URL")

    # Cache
    cache_ttl: int = Field(60, alias="CACHE_TTL")
    # Set REDIS_URL to share the cache + rate limiter across multiple worker
    # processes/instances, e.g. redis://localhost:6379/0 . Leave unset to use
    # the in-memory cache/limiter (fine for a single process — see
    # CODE_REVIEW.md item #7). `redis` (redis-py, asyncio client) must be
    # installed for this (pip install redis) — not bundled by default.
    redis_url: Optional[str] = Field(None, alias="REDIS_URL")

    # /api/analysis/ai/{symbol} and /api/strategy/recommend/{symbol} both call
    # MarketAnalyzer.get_full_market_overview() independently — without this,
    # a single Analysis-page load runs the ENTIRE fetch pipeline (spot, chain,
    # VIX, breadth, historical, global, FII/DII + option-chain snapshot save)
    # twice back-to-back, which is the main cause of the page hanging on
    # "Loading...". Short TTL so both calls in one page load reuse the same
    # snapshot, while the page still feels "live" between refreshes.
    #
    # PERF FIX (2026-08-25): 15s was too short given how expensive a live
    # fetch actually is — every Angel One SDK call in the pipeline (option
    # chain, quotes, 5min/15min/1hr candles, futures) goes through ONE
    # process-wide throttle (_MIN_CALL_INTERVAL in angel_one.py) that spaces
    # ALL calls 2.2s apart regardless of symbol, so a single full overview
    # can take 15-40+s even with zero errors, and Angel One's own rate
    # limiter still rejects some of those calls, adding 3s+ backoff-retries
    # on top. That same throttle is also shared with the background history
    # collector (run_periodic_collection, every
    # HISTORY_COLLECTOR_INTERVAL_MINUTES for NIFTY/BANKNIFTY/FINNIFTY) — so
    # a live page request can land right behind the collector's own sweep
    # and queue up behind it. With a 15s TTL and the dashboard auto-refresh
    # firing every 30s, almost every refresh missed the cache and triggered
    # a brand new live fetch, competing with the collector for the same
    # throttled connection. Raised to 45s so most 30s auto-refreshes hit the
    # cache instead — cutting live-fetch volume roughly in half without
    # making the data meaningfully stale (the finest granularity used
    # anywhere in this app is 5-minute candles).
    analysis_cache_ttl: int = Field(45, alias="ANALYSIS_CACHE_TTL")

    # Background history collector — WITHOUT this, MarketData/AnalysisResult
    # rows only get written when a human happens to have a page open (the
    # dashboard/analysis pages call save_* as a side-effect of rendering, but
    # nothing calls them on its own). That starves the Accuracy page and the
    # OI-change-over-time feature of history, which then just show "data
    # இல்லை / insufficient data" even though the app has been running for
    # hours. This background task calls the same save path once per symbol
    # on a fixed interval regardless of whether anyone is looking at the UI.
    # Set to 0 to disable (e.g. in tests) and fall back to the old
    # page-view-triggered-only behaviour.
    history_collector_interval_minutes: int = Field(5, alias="HISTORY_COLLECTOR_INTERVAL_MINUTES")
    history_collector_symbols_raw: str = Field(
        "NIFTY,BANKNIFTY,FINNIFTY", alias="HISTORY_COLLECTOR_SYMBOLS"
    )

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_file:  str = Field("./logs/app.log", alias="LOG_FILE")

    # NSE headers (fallback — curl_cffi sets its own but these stay for reference)
    nse_user_agent: str = Field(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        alias="NSE_USER_AGENT",
    )
    nse_accept: str = Field(
        "application/json, text/plain, */*", alias="NSE_ACCEPT"
    )

    # CORS
    cors_allowed_origins_raw: str = Field(
        "http://localhost:8000", alias="CORS_ALLOWED_ORIGINS"
    )

    # Rate limiting
    api_rate_limit_per_minute: int = Field(60, alias="API_RATE_LIMIT_PER_MINUTE")

    # Health / monitoring (CODE_REVIEW.md #19/#20) — optional webhook that
    # gets a JSON POST when a data source's block-rate crosses threshold
    # (see app/utils/health_metrics.py). Leave unset to just log warnings.
    alert_webhook_url: Optional[str] = Field(None, alias="ALERT_WEBHOOK_URL")

    # Risk engine — trading capital used for position sizing in
    # /api/strategy/recommend. Set DEFAULT_CAPITAL in your .env to your
    # actual trading capital. Default: ₹2,00,000 (2L).
    # BUG FIX (2026-08-16): was hardcoded 200000 inside strategy.py with a
    # "settings-இல் இருக்கணும்" comment — now actually wired through config.
    default_capital: int = Field(200000, alias="DEFAULT_CAPITAL")

    @property
    def cors_allowed_origins(self) -> List[str]:
        raw = self.cors_allowed_origins_raw.strip()
        if raw != "*":
            return [o.strip() for o in raw.split(",") if o.strip()]
        # Review: CORS_ALLOWED_ORIGINS="*" is the render.yaml default (needed
        # before the service has a URL to put there), but wide-open CORS
        # should never be the value that actually ships. Render auto-injects
        # RENDER_EXTERNAL_URL (the service's own https://xxx.onrender.com)
        # for every web service with zero extra config — using it as the
        # actual allowed origin when the raw setting is still "*" gives a
        # safe, correct default for THIS app's own same-origin UI without
        # requiring a manual post-deploy step, while still allowing an
        # explicit comma-separated override for real multi-origin setups.
        external_url = os.environ.get("RENDER_EXTERNAL_URL")
        if external_url:
            return [external_url]
        return ["*"]  # local dev / no RENDER_EXTERNAL_URL — unchanged fallback

    @property
    def history_collector_symbols(self) -> List[str]:
        return [s.strip().upper() for s in self.history_collector_symbols_raw.split(",") if s.strip()]

    model_config = {
        "env_file":          ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name":  True,   # allow field name OR alias
        "extra":             "ignore",
    }


settings = Settings()
