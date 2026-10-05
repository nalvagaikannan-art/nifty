from app.schemas import PositionsResponse
"""
Positions Router
GET  /api/portfolio/positions                  -> live positions + P&L + AI suggestion
POST /api/portfolio/positions/square-off/{sym}  -> square off a position
"""
from fastapi import APIRouter, Depends, HTTPException
import logging
import re
import time
import asyncio
from datetime import datetime

from app.services.angel_one import AngelOneSession, AngelOneError
from app.services.market_analyzer import MarketAnalyzer
from app.api.deps import get_angel_session, get_analyzer
from app.utils.helpers import days_to_expiry as _days_to_expiry

router = APIRouter()
logger = logging.getLogger(__name__)

STOP_LOSS_PCT = -20.0
TARGET_PCT    = 30.0
EXPIRY_WARN_DAYS = 3

_market_context_cache: dict = {}
_MARKET_CONTEXT_TTL = 120

# Background refresh single-flight:
# Positions must NEVER wait for the expensive full market overview.
# Only one refresh task per underlying root may run at a time.
_market_context_refresh_tasks: dict[str, asyncio.Task] = {}


async def _refresh_market_context(analyzer: MarketAnalyzer, root: str) -> dict:
    """Fetch and cache market context without blocking the positions response."""
    try:
        logger.info(f"MARKET_CTX_BG_REFRESH_START root={root}")
        md = await analyzer.get_full_market_overview(root)
        expiry_str = md.get("option_chain", {}).get("expiry", "")
        md["days_to_expiry"] = _days_to_expiry(expiry_str)

        # TTL starts after the expensive fetch completes.
        _market_context_cache[root] = (time.time(), md)
        logger.info(f"MARKET_CTX_BG_REFRESH_DONE root={root}")
        return md
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Market overview background refresh failed for {root}: {e}")
        return {}


def _schedule_market_context_refresh(
    analyzer: MarketAnalyzer,
    root: str,
) -> None:
    """Start at most one background market-context refresh for this root."""
    existing = _market_context_refresh_tasks.get(root)

    if existing is not None and not existing.done():
        logger.info(f"MARKET_CTX_BG_REFRESH_ALREADY_RUNNING root={root}")
        return

    task = asyncio.create_task(_refresh_market_context(analyzer, root))
    _market_context_refresh_tasks[root] = task

    def _cleanup(done_task: asyncio.Task, _root: str = root) -> None:
        current = _market_context_refresh_tasks.get(_root)
        if current is done_task:
            _market_context_refresh_tasks.pop(_root, None)

    task.add_done_callback(_cleanup)


def _market_context_for_positions(
    analyzer: MarketAnalyzer,
    root: str,
) -> tuple[dict, float | None]:
    """
    Fast, non-blocking market-context lookup for the positions endpoint.

    - Fresh cache: use it.
    - Stale cache: use stale data immediately and refresh in background.
    - Missing cache: return empty context immediately and refresh in background.
    """
    now = time.time()
    cached = _market_context_cache.get(root)

    if cached:
        cached_at, market = cached
        age = max(0.0, now - cached_at)

        if age < _MARKET_CONTEXT_TTL:
            logger.info(
                f"MARKET_CTX_FAST_HIT root={root} age={age:.1f}s"
            )
            return market, age

        logger.info(
            f"MARKET_CTX_STALE_FAST root={root} age={age:.1f}s "
            f"using_stale_without_refresh=true"
        )
        # IMPORTANT:
        # Never start get_full_market_overview() from the positions request.
        # That expensive call shares the global Angel One throttle and can
        # delay live LTP/P&L and other market-analysis requests.
        return market, age

    logger.info(
        f"MARKET_CTX_MISS_FAST root={root} "
        f"using_empty_context_without_refresh=true"
    )
    # Positions must remain a broker-LTP/P&L fast path.
    # _ai_suggestion() can safely operate with an empty market context:
    # stop-loss/target and position-encoded expiry are still evaluated.
    return {}, None


_WARM_ROOTS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
_WARM_INTERVAL_SECONDS = 45


async def warm_market_context_cache(analyzer: MarketAnalyzer) -> None:
    """
    Optional legacy warmer.

    This function is intentionally NOT started by the positions route.
    The fast positions path refreshes only roots that are actually needed.
    """
    while True:
        for root in _WARM_ROOTS:
            try:
                await _get_market_context(analyzer, root)
            except Exception as e:
                logger.warning(f"Cache warmer failed for {root}: {e}")
        await asyncio.sleep(_WARM_INTERVAL_SECONDS)


async def _get_market_context(analyzer: MarketAnalyzer, root: str) -> dict:
    """Compatibility helper for callers that explicitly need a blocking fetch."""
    now = time.time()
    cached = _market_context_cache.get(root)

    if cached and (now - cached[0]) < _MARKET_CONTEXT_TTL:
        return cached[1]

    md = await _refresh_market_context(analyzer, root)
    return md


def _compute_pnl(pos: dict) -> dict:
    qty       = pos["netqty"]
    avg_price = pos["averageprice"]
    ltp       = pos["lasttradedprice"]
    side      = pos["buysell"]

    direction = 1 if side == "BUY" else -1
    pnl = direction * (ltp - avg_price) * qty
    status = "PROFIT" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"

    return {
        "symbol":     pos["tradingsymbol"],
        "quantity":   qty,
        "avg_price":  avg_price,
        "ltp":        ltp,
        "side":       side,
        "pnl":        round(pnl, 2),
        "status":     status,
    }


def _root_symbol(tradingsymbol: str) -> str:
    m = re.match(r"^[A-Z]+", tradingsymbol or "")
    return m.group() if m else "NIFTY"


def _position_days_to_expiry(tradingsymbol: str):
    """Extract DDMMMYY expiry from an option tradingsymbol and return DTE."""
    m = re.search(r"([0-9]{2}[A-Z]{3}[0-9]{2})", tradingsymbol or "")
    if not m:
        return None

    try:
        expiry = datetime.strptime(m.group(1).title(), "%d%b%y")
        expiry_str = expiry.strftime("%d%b%Y")
        return _days_to_expiry(expiry_str)
    except ValueError:
        return None


def _ai_suggestion(p: dict, market: dict) -> dict:
    reasons = []
    invested = p["avg_price"] * p["quantity"]
    change_pct = round((p["pnl"] / invested) * 100, 2) if invested > 0 else 0.0
    verdict = "HOLD"

    if invested <= 0:
        return {"verdict": verdict, "change_pct": 0.0, "reasons": ["Invested amount unknown"],
                "days_to_expiry": None, "vix": 0, "pcr": 0, "market_bias": "N/A"}

    if change_pct <= STOP_LOSS_PCT:
        verdict = "EXIT"
        reasons.append(f"Stop-loss level ({STOP_LOSS_PCT}%) reached - current {change_pct}%")
    elif change_pct >= TARGET_PCT:
        verdict = "EXIT"
        reasons.append(f"Target ({TARGET_PCT}%) reached, consider booking profit - current {change_pct}%")
    else:
        reasons.append(f"Cost-basis change {change_pct}% - within stop-loss/target range")

    # Prefer the actual expiry encoded in the held position symbol.
    # Fall back to current market expiry only if the symbol cannot be parsed.
    dte = _position_days_to_expiry(p.get("symbol", ""))
    if dte is None:
        dte = market.get("days_to_expiry")
    if dte is not None:
        if dte <= 1:
            if verdict == "HOLD":
                verdict = "CAUTION"
            reasons.append(f"Only {dte} day(s) to expiry - theta decay accelerates sharply")
        elif dte <= EXPIRY_WARN_DAYS:
            reasons.append(f"{dte} days to expiry - theta decay picking up")

    dec = market.get("decision", {}) or {}
    bias = dec.get("market_bias", "Sideways")
    reasons.append(f"Market bias: {bias} (Confidence {dec.get('confidence', 0)}%)")

    vix = market.get("vix", 0)
    pcr = market.get("pcr", 0)
    if vix:
        reasons.append(f"India VIX: {vix} ({'LOW - calm market' if vix < 15 else 'HIGH - volatile, elevated risk'})")
    if pcr:
        if pcr < 0.8:
            pcr_label = "Bearish"
        elif pcr <= 1.2:
            pcr_label = "Neutral"
        else:
            pcr_label = "Bullish"
        reasons.append(f"PCR: {pcr} ({pcr_label})")

    return {
        "verdict":     verdict,
        "change_pct":  change_pct,
        "reasons":     reasons,
        "days_to_expiry": dte,
        "vix":         vix,
        "pcr":         pcr,
        "market_bias": bias,
    }


@router.get("/positions", response_model=PositionsResponse)
async def get_positions(
    angel: AngelOneSession = Depends(get_angel_session),
    analyzer: MarketAnalyzer = Depends(get_analyzer),
):
    try:
        raw_positions = await angel.get_positions()
    except AngelOneError as e:
        logger.error(f"get_positions failed: {e}")
        raise HTTPException(502, detail=f"Positions fetch failed: {e}")

    result = []
    total_pnl = 0.0
    for pos in raw_positions:
        # FAST PATH:
        # Broker LTP/P&L must never wait for the expensive market overview.
        enriched = _compute_pnl(pos)
        root = _root_symbol(pos.get("tradingsymbol", ""))

        # FASTEST POSITION PATH:
        # Never fetch/schedule a full market overview from the positions endpoint.
        # Live broker LTP/P&L + position expiry are sufficient for the actionable
        # EXIT/HOLD decision. Market context is optional enrichment only.
        market, market_age = {}, None

        suggestion = _ai_suggestion(enriched, market)
        enriched["ai_suggestion"]  = suggestion["verdict"]
        enriched["ai_reasons"]     = suggestion["reasons"]
        enriched["change_pct"]     = suggestion["change_pct"]
        enriched["days_to_expiry"] = suggestion.get("days_to_expiry")
        enriched["vix"]            = suggestion.get("vix")
        enriched["pcr"]            = suggestion.get("pcr")
        enriched["market_bias"]    = suggestion.get("market_bias")
        enriched["stop_loss_hit"]  = (
            suggestion["verdict"] == "EXIT"
            and suggestion["change_pct"] <= STOP_LOSS_PCT
        )
        enriched["market_data_age_seconds"] = (
            round(market_age, 1) if market_age is not None else None
        )

        total_pnl += enriched["pnl"]
        result.append(enriched)

    return {
        "positions": result,
        "total_pnl": round(total_pnl, 2),
        "count":     len(result),
        "disclaimer": "AI suggestion is a rule-based hint only (P&L%, VIX, PCR, market bias, expiry proximity) - not investment advice. Final decision is yours.",
    }


@router.post("/positions/square-off/{symbol}")
async def square_off_position(symbol: str, angel: AngelOneSession = Depends(get_angel_session)):
    # This app is analysis/information only by design -- it never places,
    # modifies, or squares off real orders with the broker (see
    # angel_one.py's note above AngelOneSession.get_positions()). Exit this
    # position directly in the Angel One app or website.
    raise HTTPException(
        403,
        detail=(
            f"Square off is disabled in this app for safety -- it never places "
            f"real orders. Please exit {symbol} directly in the Angel One app "
            f"or website."
        ),
    )
