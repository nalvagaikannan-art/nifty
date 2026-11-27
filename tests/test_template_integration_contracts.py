"""Cross-check Jinja templates and the API routes consumed by primary pages.

These are structural integration checks: they catch template parse errors and
frontend/backend route drift without requiring live broker credentials or
market hours.
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.main import app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "app" / "templates"


def test_every_html_template_compiles():
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    templates = sorted(TEMPLATE_DIR.glob("*.html"))

    assert templates, "No HTML templates found"

    for path in templates:
        # get_template parses the complete file, including inherited blocks,
        # inline styles, and embedded JavaScript text.
        env.get_template(path.name)


def test_primary_page_api_contracts_are_registered():
    registered_paths = {
        route.path
        for route in app.routes
        if isinstance(getattr(route, "path", None), str)
    }

    # Active Dashboard + Analysis + MCX workspace calls (not comment-only URLs).
    required_paths = {
        "/api/dashboard/status",
        "/api/market/symbols",
        "/api/market/universe",
        "/api/market/fno-stock-scan",
        "/api/market/mcx/catalog",
        "/api/market/mcx/contracts",
        "/api/market/mcx/quote/{commodity}",
        "/api/market/mcx/analysis/{commodity}",
        "/api/market/mcx/candles/{commodity}",
        "/api/angel/live-feed",
        "/api/portfolio/positions",
        "/api/paper-trade/open",
        "/api/analysis/ai/{symbol}",
        "/api/strategy/recommend/{symbol}",
    }

    missing = sorted(required_paths - registered_paths)
    assert not missing, (
        "A primary HTML template calls API routes that are not registered: "
        + ", ".join(missing)
    )

def test_mcx_backend_payload_fields_match_workspace_template():
    """Exercise the backend payload keys consumed by the MCX workspace JS."""
    import asyncio
    from unittest.mock import AsyncMock

    from app.services.angel_one import AngelOneSession

    async def run():
        session = AngelOneSession.__new__(AngelOneSession)
        session._instruments = [
            {
                "name": "GOLD", "expiry": "26FEB2027",
                "symbol": "GOLD26FEB27FUT", "instrumenttype": "FUTCOM",
                "exch_seg": "MCX", "tick_size": "50", "lotsize": "1",
                "token": "201",
            },
            {
                "name": "GOLD", "expiry": "26FEB2027",
                "strike": "100000", "symbol": "GOLD26FEB27100000CE",
                "instrumenttype": "OPTFUT", "exch_seg": "MCX",
                "tick_size": "50", "lotsize": "1", "token": "202",
            },
            {
                "name": "GOLD", "expiry": "26FEB2027",
                "strike": "100000", "symbol": "GOLD26FEB27100000PE",
                "instrumenttype": "OPTFUT", "exch_seg": "MCX",
                "tick_size": "50", "lotsize": "1", "token": "203",
            },
        ]
        session.ensure_session = AsyncMock()
        session._ensure_instruments = AsyncMock()

        catalog = await session.get_mcx_commodity_catalog()
        assert catalog["available"] is True
        gold = next(row for row in catalog["commodities"] if row["commodity"] == "GOLD")
        assert {
            "commodity", "futures_contracts", "options_contracts",
            "option_types", "options_available",
        } <= gold.keys()
        assert gold["futures_contracts"] == 1
        assert gold["options_contracts"] == 2
        assert gold["option_types"] == ["CE", "PE"]
        assert gold["options_available"] is True

        contracts = await session.get_mcx_futures_contracts("GOLD")
        assert len(contracts) == 1
        contract = contracts[0]
        assert {
            "tradingsymbol", "expiry", "lot_size", "tick_size",
            "token", "instrumenttype", "exchange",
        } <= contract.keys()

    asyncio.run(run())

def test_mcx_analysis_template_fields_fail_closed_without_candles():
    """The MCX UI must receive explicit WAIT fields when candles are absent."""
    from app.services.mcx_analysis import analyze_mcx_futures

    result = analyze_mcx_futures(
        [],
        {
            "name": "GOLD",
            "tradingsymbol": "GOLD26FEB27FUT",
            "tick_size": 0.5,
            "lot_size": 1,
        },
    )

    # These are the fields read by analysis.html's MCX analyze() handler.
    assert result["action"] == "WAIT"
    assert result["eligible"] is False
    assert isinstance(result["reason"], str) and result["reason"]
    assert result["exchange"] == "MCX"
    assert result["instrument"] == "GOLD"
    assert result["source"] == "angel_one_mcx_candles"
    assert "entry_reference" not in result or result["entry_reference"] is None
    assert "stop_reference" not in result or result["stop_reference"] is None
    assert "target_reference" not in result or result["target_reference"] is None
    assert result.get("execution_enabled") is not True

def test_mcx_terminal_ui_labels_separate_futures_signals_from_options_catalogue():
    """Prevent UI copy from implying OPTFUT catalogue rows are live option signals."""
    template = (TEMPLATE_DIR / "analysis.html").read_text(encoding="utf-8")

    required_copy = (
        "MCX Trading Terminal",
        "ANALYSIS / PAPER",
        "EXECUTION",
        "MCX FUTURES ONLY",
        "Futures Analysis · FUTCOM",
        "Options Catalogue · OPTFUT",
        "no live option-chain analysis",
        "Greeks",
        "broker order placement இல்லை",
    )
    for phrase in required_copy:
        assert phrase in template, f"Missing MCX terminal safety/UI label: {phrase}"

    assert 'id="mx-action" data-action="wait"' in template
    assert "dataset.action=" in template

def test_dashboard_system_health_strip_uses_backend_status_and_labels_check_time():
    """Status badges are backend-derived; client time is only the check timestamp."""
    template = (TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert 'id="lt-status-ai"' in template
    assert 'id="lt-status-angel"' in template
    assert 'id="lt-status-market"' in template
    assert 'id="lt-status-checked"' in template
    assert "fetch('/api/dashboard/status')" in template
    assert "s.ai_provider_configured" in template
    assert "s.angel_one_connected" in template
    assert "s.market_open" in template
    assert "Status check failed" in template
    assert "Backend status check time, not quote freshness" in template

def test_dashboard_signal_history_panel_uses_selected_symbol_persisted_api():
    template = (TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert 'id="lt-signal-history"' in template
    assert 'id="lt-history-symbol"' in template
    assert "function ltLoadSignalHistory()" in template
    assert "fetch('/api/strategy/history/' + encodeURIComponent(sym)" in template
    assert "payload.history" in template
    assert "item.strategy" in template
    assert "item.reversal" in template
    assert "item.reversal_type" in template
    assert "item.reasons" in template
    assert "slice(0,2)" in template
    assert "ltLoadSignalHistory();" in template
    assert "This is history, not a live entry instruction." in template


def test_dashboard_watchlist_uses_only_backend_verified_symbols_and_existing_symbol_flow():
    template = (TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert 'id="lt-watchlist-items"' in template
    assert "fetch('/api/market/symbols')" in template
    assert "Array.isArray(u.indices)" in template
    assert "Array.isArray(u.stocks)" in template
    assert "u.stocks_available" in template
    assert "chip.addEventListener('click',function(){ltSetSym(null,sym);});" in template
    assert "ltUpdateWatchlistSelection();" in template
    assert "does not imply a live quote" in template
    assert "Broker-verified symbols unavailable" in template
    assert "Verified index symbols unavailable" in template


def test_dashboard_watchlist_keeps_indices_when_stock_master_is_unavailable_and_supports_search():
    template = (TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert 'id="lt-watch-search"' in template
    assert "var ltWatchSymbols=[];" in template
    assert "if(!u || !Array.isArray(u.indices)) throw new Error('Invalid symbol universe payload');" in template
    assert "ltWatchSymbols=Array.from(new Set(indices.concat(stocks)));" in template
    assert "u.stocks_available===true" in template
    assert "indices only" in template
    assert "ltWatchSymbols.filter(function(sym){return !q || sym.indexOf(q)!==-1;})" in template
    assert "No matching symbols" in template


def test_market_symbol_universe_returns_indices_and_broker_verified_stocks(monkeypatch):
    """The Dashboard watchlist payload must expose the documented universe shape."""
    import asyncio
    from unittest.mock import AsyncMock

    from app.services import angel_one
    from app.services.data_fetcher import DataFetcher

    class BrokerSession:
        is_configured = True
        get_fno_stock_symbols = AsyncMock(return_value=["RELIANCE", "TCS"])

    monkeypatch.setattr(angel_one, "angel_session", BrokerSession())
    payload = asyncio.run(DataFetcher.__new__(DataFetcher).get_market_symbol_universe())

    assert payload == {
        "indices": ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"],
        "stocks": ["RELIANCE", "TCS"],
        "stocks_available": True,
    }


def test_market_symbol_universe_fails_back_to_indices_when_broker_unavailable(monkeypatch):
    """A missing broker session must not remove the supported index watchlist."""
    import asyncio

    from app.services import angel_one
    from app.services.data_fetcher import DataFetcher

    class BrokerSession:
        is_configured = False

    monkeypatch.setattr(angel_one, "angel_session", BrokerSession())
    payload = asyncio.run(DataFetcher.__new__(DataFetcher).get_market_symbol_universe())

    assert payload == {
        "indices": ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"],
        "stocks": [],
        "stocks_available": False,
    }


def test_dashboard_shows_market_data_unavailable_as_wait_with_backend_detail():
    template = (TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert "err.status = r.status;" in template
    assert "err.detail = payload && (payload.detail || payload.error);" in template
    assert "var marketUnavailable = r.status === 502 || r.status === 503;" in template
    assert "WAIT · UNAVAILABLE" in template
    assert "Market data unavailable — WAIT" in template
    assert "ltEsc(unavailableMessage)" in template


def test_market_data_unavailable_uses_service_unavailable_status():
    import asyncio
    import json
    from starlette.requests import Request

    from app.exceptions import MarketDataError
    from app.main import market_data_error_handler

    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/strategy/recommend/NIFTY",
        "raw_path": b"/api/strategy/recommend/NIFTY",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    })
    response = asyncio.run(
        market_data_error_handler(
            request,
            MarketDataError("No persisted trading-session spot available"),
        )
    )

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["error"] == "market_data_unavailable"
    assert "No persisted trading-session spot" in payload["detail"]


def test_strategy_and_analysis_market_data_exceptions_are_not_bad_gateway():
    strategy = (ROOT / "app" / "api" / "routes" / "strategy.py").read_text(encoding="utf-8")
    analysis = (ROOT / "app" / "api" / "routes" / "analysis.py").read_text(encoding="utf-8")

    assert 'raise HTTPException(503, detail=f"Market data unavailable: {e}")' in strategy
    assert 'raise HTTPException(503, detail=f"Market data unavailable: {e}")' in analysis



def test_dashboard_preserves_strategy_error_status_and_backend_detail():
    template = (TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert "status:item.status || null" in template
    assert "detail:item.detail || ''" in template
    assert "Market data unavailable — WAIT" in template
    assert "r.detail || r.message || 'Unknown request error'" in template
    assert "err.detail = payload && (payload.detail || payload.error)" in template


def test_analysis_displays_strategy_failure_status_and_backend_detail():
    template = (TEMPLATE_DIR / "analysis.html").read_text(encoding="utf-8")

    assert "Strategy data unavailable" in template
    assert "HTTP ' + x.status" in template
    assert "payload.detail || payload.error" in template
    assert "Signal history unavailable — ' + failure" in template


def test_analysis_labels_positions_as_read_only_angel_one_broker_positions():
    template = (TEMPLATE_DIR / "analysis.html").read_text(encoding="utf-8")
    positions_route = (ROOT / "app" / "api" / "routes" / "positions.py").read_text(encoding="utf-8")

    assert "ANGEL ONE BROKER POSITIONS · READ ONLY" in template
    assert "not paper-trade records" in template
    assert "authenticated Angel One positions API" in template
    assert "raw_positions = await angel.get_positions()" in positions_route
    assert "Square off is disabled in this app" in positions_route
