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
from app.api.routes import settings as settings_routes
from app.api.routes import paper_trade as paper_trade_routes
from app.utils.logging import setup_logging
from app.services.data_fetcher import DataFetcher
import app.services.paper_trading  # noqa: F401 — registers PaperTrade + TradeJournal models
from app.services.market_analyzer import MarketAnalyzer
from app.services.history_collector import run_periodic_collection
from app.exceptions import MarketDataError, AIProviderError
from app.middleware.rate_limit import RateLimitMiddleware
import logging

logger = logging.getLogger(__name__)


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

    if angel_session.is_configured:
        app.state.angel_warmup_task = asyncio.create_task(
            angel_session.warmup_instruments()
        )
    else:
        app.state.angel_warmup_task = None
    yield
    logger.info("Shutting down...")
    app.state.history_collector_task.cancel()
    try:
        await app.state.history_collector_task
    except asyncio.CancelledError:
        pass
    if app.state.angel_warmup_task and not app.state.angel_warmup_task.done():
        app.state.angel_warmup_task.cancel()
        try:
            await app.state.angel_warmup_task
        except asyncio.CancelledError:
            pass
    await app.state.data_fetcher.close()
    from app.services.global_market import global_market_service
    from app.services.economic_calendar import economic_calendar_service
    await global_market_service.close()
    await economic_calendar_service.close()

app = FastAPI(
    title="AI NIFTY Option Analyzer Pro",
    description="Live Market Analysis and AI Decision Support Tool",
    version="1.0.0",
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
