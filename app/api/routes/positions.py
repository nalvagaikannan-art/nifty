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


async def _get_market_context(analyzer: MarketAnalyzer, root: str) -> dict:
    now = time.time()
    logger.info(f"MARKET_CTX_CALL root={root}")
    cached = _market_context_cache.get(root)

    if cached and (now - cached[0]) < _MARKET_CONTEXT_TTL:
        logger.info(
            f"MARKET_CTX_CACHE_HIT root={root} age={now - cached[0]:.1f}s"
        )
        return cached[1]

    try:
        md = await analyzer.get_full_market_overview(root)
        expiry_str = md.get("option_chain", {}).get("expiry", "")
        md["days_to_expiry"] = _days_to_expiry(expiry_str)
    except Exception as e:
        logger.warning(f"Market overview fetch failed for {root}: {e}")
        md = {}

    # IMPORTANT: start TTL after the expensive fetch completes.
    _market_context_cache[root] = (time.time(), md)
    return md


_WARM_ROOTS = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
_WARM_INTERVAL_SECONDS = 45


async def warm_market_context_cache(analyzer: MarketAnalyzer) -> None:
    while True:
        for root in _WARM_ROOTS:
            try:
                await _get_market_context(analyzer, root)
            except Exception as e:
                logger.warning(f"Cache warmer failed for {root}: {e}")
        await asyncio.sleep(_WARM_INTERVAL_SECONDS)


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


@router.get("/positions")
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
        enriched = _compute_pnl(pos)
        root = _root_symbol(pos.get("tradingsymbol", ""))
        market = await _get_market_context(analyzer, root)

        suggestion = _ai_suggestion(enriched, market)
        enriched["ai_suggestion"]  = suggestion["verdict"]
        enriched["ai_reasons"]     = suggestion["reasons"]
        enriched["change_pct"]     = suggestion["change_pct"]
        enriched["days_to_expiry"] = suggestion.get("days_to_expiry")
        enriched["vix"]            = suggestion.get("vix")
        enriched["pcr"]            = suggestion.get("pcr")
        enriched["market_bias"]    = suggestion.get("market_bias")
        enriched["stop_loss_hit"]  = suggestion["verdict"] == "EXIT" and suggestion["change_pct"] <= STOP_LOSS_PCT
        cached_entry = _market_context_cache.get(root)
        enriched["market_data_age_seconds"] = round(time.time() - cached_entry[0], 1) if cached_entry else None

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
