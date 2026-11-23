import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import engine, Base
from app.api.routes import dashboard, market, options, analysis, angel, positions, strategy, market_intel, accuracy
from app.api.routes import pro as pro_routes  # NIFTY-PRO
import app.services.iv_history  # noqa: F401 — registers pro_iv_history table
import app.services.order_executor  # noqa: F401 — registers pro_orders table
from app.api.routes import settings as settings_routes
from app.api.routes import paper_trade as paper_trade_routes
from app.utils.logging import setup_logging
from app.services.data_fetcher import DataFetcher
import app.services.paper_trading  # noqa: F401 — registers PaperTrade + TradeJournal models
from app.services.market_analyzer import MarketAnalyzer, _snapshot_persistence_enabled
from app.services.history_collector import (
    run_periodic_collection,
    run_periodic_live_market_snapshots,
    run_periodic_option_chain_snapshots,
    run_periodic_paper_capture,
    run_periodic_forward_paper_outcomes,
)
from app.exceptions import MarketDataError, AIProviderError
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.auth import AppAuthMiddleware
import logging

logger = logging.getLogger(__name__)


ANGEL_LIVE_FEED_RETRY_INTERVAL_SEC = 60


async def _start_angel_live_feed_with_retry(angel_live_feed):
    """Retry initial Angel WebSocket startup only during NSE market hours."""
    while True:
        if not is_market_hours_ist():
            await asyncio.sleep(ANGEL_LIVE_FEED_RETRY_INTERVAL_SEC)
            continue

        try:
            started = await angel_live_feed.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Angel live feed startup attempt failed; REST remains available: %s",
                exc,
            )
        else:
            if started:
                logger.info("Angel live feed startup succeeded")
                return
            logger.warning(
                "Angel live feed startup returned false; retrying in %ss",
                ANGEL_LIVE_FEED_RETRY_INTERVAL_SEC,
            )

        await asyncio.sleep(ANGEL_LIVE_FEED_RETRY_INTERVAL_SEC)


class _HealthCheckFilter(logging.Filter):
    """Drops uvicorn access-log lines for the bare health-check path '/' —
    Render polls this every ~5s and it drowns out real request/app logs.
    Real user traffic (e.g. GET /dashboard, GET /api/...) still logs normally."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not (' "GET / HTTP' in msg or ' "HEAD / HTTP' in msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # Must run AFTER setup_logging() — if setup_logging() reconfigures
    # uvicorn.access's handlers, a filter attached before that call could
    # be dropped along with them.
    logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())
    logger.info("Starting AI NIFTY Option Analyzer Pro")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # SQLite: safer concurrent reads/writes while dashboard is polling.
        if engine.url.drivername == "sqlite+aiosqlite":
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
    fetcher = DataFetcher()
    app.state.data_fetcher = fetcher
    app.state.market_analyzer = MarketAnalyzer(fetcher=fetcher)
    # Runs independently of any browser tab so Accuracy-page / OI-history
    # data keeps accumulating even with zero page views — see
    # app/services/history_collector.py for why this was needed.
    app.state.history_collector_task = asyncio.create_task(
        run_periodic_collection(app.state.market_analyzer)
    )

    # Lightweight market-hours writer fed only by the existing Angel
    # WebSocket LTP memory. This fills MarketData history even when no
    # browser/API request is active and adds no broker REST calls.
    app.state.live_market_snapshot_task = asyncio.create_task(
        run_periodic_live_market_snapshots()
    )

    # Capture option chains independently of browser/API traffic.
    app.state.option_chain_snapshot_task = asyncio.create_task(
        run_periodic_option_chain_snapshots(app.state.market_analyzer)
    )
    # Prospective paper observation uses the shared strategy route.
    app.state.paper_capture_task = asyncio.create_task(
        run_periodic_paper_capture(app.state.market_analyzer)
    )
    # Grades persisted forward observations; it never places orders.
    app.state.forward_paper_outcome_task = asyncio.create_task(
        run_periodic_forward_paper_outcomes()
    )
    # FIX (2026-08-21): Angel One's option-chain/futures-premium calls both
    # need the ~15-30MB instrument master file (see angel_one._ensure_
    # instruments). Previously that file was only downloaded lazily, on
    # whichever request happened to touch it first — under Render's network
    # that cold download could exceed the old 30s timeout and fail with an
    # empty-message exception, taking option chain + futures premium down
    # together and forcing a fallback to NSE (which then often 404s too).
    # Pre-downloading it here, in the background, means it's already cached
    # by the time the first real dashboard/analysis request comes in.
    # Fire-and-forget: warmup_instruments() catches its own errors and logs
    # a warning rather than raising, so a failed warmup just means the
    # instrument master gets downloaded lazily on first use as before —
    # startup is never blocked or failed by this.
    
    from app.services.angel_one import angel_session
    from app.services.angel_live_feed import angel_live_feed

    if angel_session.is_configured:
        # Retry initial login/feed startup independently; REST remains available.
        # The retry coroutine only attempts login during NSE market hours and
        # exits permanently once the feed workers have started successfully.
        app.state.angel_live_feed_startup_task = asyncio.create_task(
            _start_angel_live_feed_with_retry(angel_live_feed)
        )

        # FIX (2026-08-26): warmup_instruments() downloads 36MB instrument
        # master. Running it immediately at startup (same moment as
        # history_collector first tick + DB init + NSE session) caused a
        # memory/CPU spike that tripped Render's health-check timeout,
        # causing the process to restart ~2 min after every deploy.
        # Delay by 15s so the server is fully up and health-check passes
        # before the large download begins.
        async def _delayed_warmup():
            await asyncio.sleep(15)
            await angel_session.warmup_instruments()

            # Prewarm the same foreground NIFTY overview cache used by
            # Strategy/Analysis requests, so the first request avoids the
            # cold Angel fetch latency.
            try:
                  token = _snapshot_persistence_enabled.set(False)
                  try:
                      logger.info("Startup prewarm: NIFTY market overview starting")
                      await app.state.market_analyzer.get_full_market_overview(
                          "NIFTY",
                          expiry=None,
                      )
                      logger.info("Startup prewarm: NIFTY market overview completed")
                  finally:
                      _snapshot_persistence_enabled.reset(token)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Startup prewarm: NIFTY market overview failed: %s",
                    e,
                )

        app.state.angel_warmup_task = asyncio.create_task(_delayed_warmup())
    else:
        app.state.angel_warmup_task = None
    yield
    logger.info("Shutting down...")
    app.state.history_collector_task.cancel()
    try:
        await app.state.history_collector_task
    except asyncio.CancelledError:
        pass

    app.state.live_market_snapshot_task.cancel()
    try:
        await app.state.live_market_snapshot_task
    except asyncio.CancelledError:
        pass

    app.state.option_chain_snapshot_task.cancel()
    try:
        await app.state.option_chain_snapshot_task
    except asyncio.CancelledError:
        pass

    app.state.paper_capture_task.cancel()
    try:
        await app.state.paper_capture_task
    except asyncio.CancelledError:
        pass

    app.state.forward_paper_outcome_task.cancel()
    try:
        await app.state.forward_paper_outcome_task
    except asyncio.CancelledError:
        pass

    if app.state.angel_warmup_task and not app.state.angel_warmup_task.done():
        app.state.angel_warmup_task.cancel()
        try:
            await app.state.angel_warmup_task
        except asyncio.CancelledError:
            pass
    # Stop pending startup retries before stopping feed workers.
    angel_startup_task = getattr(
        app.state, "angel_live_feed_startup_task", None
    )
    if angel_startup_task and not angel_startup_task.done():
        angel_startup_task.cancel()
        try:
            await angel_startup_task
        except asyncio.CancelledError:
            pass
    # Stop Angel WebSocket before closing the REST/data services.
    try:
        stop_completed = await asyncio.to_thread(angel_live_feed.stop)
        if not stop_completed:
            logger.warning(
                "Angel live feed shutdown returned with worker threads still active"
            )
    except Exception as e:
        logger.warning("Angel live feed shutdown failed: %s", e)

    await app.state.data_fetcher.close()
    from app.services.global_market import global_market_service
    from app.services.economic_calendar import economic_calendar_service
    await global_market_service.close()
    await economic_calendar_service.close()

app = FastAPI(
    title="AI NIFTY Option Analyzer PRO+",
    description="Live Market Analysis and AI Decision Support Tool",
    version="2.0.0-pro",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
# Review: visible confirmation of what CORS actually resolved to at startup
# — settings.cors_allowed_origins already auto-narrows a raw "*" down to
# RENDER_EXTERNAL_URL when that's set (see app/config.py), but if it's
# STILL "*" here (no RENDER_EXTERNAL_URL — e.g. a non-Render host), that's
# worth a loud log line rather than a silently-permissive API.
if settings.cors_allowed_origins == ["*"]:
    logger.warning(
        "CORS_ALLOWED_ORIGINS resolved to '*' — any website can call this API "
        "from a browser. Set CORS_ALLOWED_ORIGINS to your actual domain(s) "
        "explicitly if this is a production deployment not on Render."
    )
else:
    logger.info(f"CORS allowed origins: {settings.cors_allowed_origins}")
app.add_middleware(RateLimitMiddleware, requests_per_minute=settings.api_rate_limit_per_minute)
# NIFTY-PRO: protect every page, API and static asset; only /healthz is public.
app.add_middleware(AppAuthMiddleware)


@app.exception_handler(MarketDataError)
async def market_data_error_handler(request: Request, exc: MarketDataError):
    logger.error(f"Unhandled MarketDataError on {request.url.path}: {exc}")
    return JSONResponse(status_code=502, content={"error": "market_data_unavailable", "detail": str(exc)})


@app.exception_handler(AIProviderError)
async def ai_provider_error_handler(request: Request, exc: AIProviderError):
    logger.error(f"Unhandled AIProviderError on {request.url.path}: {exc}")
    return JSONResponse(status_code=503, content={"error": "ai_provider_unavailable", "detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception on {request.url.path}")
    return JSONResponse(status_code=500, content={"error": "internal_server_error", "detail": "An unexpected error occurred."})


app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Routes
app.include_router(dashboard.router,       prefix="/api/dashboard",  tags=["Dashboard"])
app.include_router(market.router,          prefix="/api/market",     tags=["Market"])
app.include_router(options.router,         prefix="/api/options",    tags=["Options"])
app.include_router(analysis.router,        prefix="/api/analysis",   tags=["Analysis"])
app.include_router(settings_routes.router, prefix="/api/settings",   tags=["Settings"])
app.include_router(angel.router,           prefix="/api/angel",      tags=["Angel One"])
app.include_router(positions.router,       prefix="/api/portfolio",  tags=["Portfolio"])
app.include_router(strategy.router,        prefix="/api/strategy",   tags=["Strategy"])
app.include_router(market_intel.router,    prefix="/api/market-intel", tags=["Market Intelligence"])
app.include_router(paper_trade_routes.router, prefix="/api/paper-trade", tags=["Paper Trading"])
app.include_router(accuracy.router,        prefix="/api/accuracy",   tags=["Accuracy"])
app.include_router(pro_routes.router,      prefix="/api/pro",        tags=["Pro (sell signals / orders)"])

# Public Render health probe; no application data is exposed.
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# Frontend pages
@app.get("/sw.js")
async def service_worker():
    from fastapi.responses import FileResponse
    return FileResponse("app/static/sw.js", media_type="application/javascript")


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/dashboard")
async def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/terminal")
async def terminal_page(request: Request):
    return templates.TemplateResponse("terminal.html", {"request": request})


@app.get("/live")
async def live_page(request: Request):
    return templates.TemplateResponse("live.html", {"request": request})


@app.get("/option-view")
async def option_view_page(request: Request):
    return templates.TemplateResponse("option-view.html", {"request": request})


@app.get("/market")
async def market_page(request: Request):
    return templates.TemplateResponse("market.html", {"request": request})


@app.get("/options")
async def options_page(request: Request):
    return templates.TemplateResponse("options.html", {"request": request})


@app.get("/analysis")
async def analysis_page(request: Request):
    return templates.TemplateResponse("analysis.html", {"request": request})


@app.get("/settings")
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/accuracy")
async def accuracy_page(request: Request):
    return templates.TemplateResponse("accuracy.html", {"request": request})


@app.get("/positions")
async def positions_page(request: Request):
    return templates.TemplateResponse("positions.html", {"request": request})

# CALL_TRACKER_PAGE_20261026
@app.get("/call-tracker")
async def call_tracker_page(request: Request):
    return templates.TemplateResponse("call-tracker.html", {"request": request})


# NIFTY-PRO page
@app.get("/pro")
async def pro_page(request: Request):
    return templates.TemplateResponse("pro.html", {"request": request})
