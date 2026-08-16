"""
Positions Router
─────────────────
GET  /api/portfolio/positions                  → live positions + P&L + AI suggestion
POST /api/portfolio/positions/square-off/{sym}  → square off a position

angel_one.py மற்றும் deps.py உங்கள் actual files-ல் இருந்து பார்த்து
integrate பண்ணப்பட்டது:
  - AngelOneSession.get_positions() / .square_off_position() (angel_one.py-ல் புதிதா சேர்க்கப்பட்டது)
  - get_angel_session() dependency (deps.py-ல் புதிதா சேர்க்கப்பட்டது)

⚠️ get_positions()/square_off_position()-ல் இருக்கும் field names
(netqty, avgnetprice, ltp) Angel's public docs அடிப்படையில் — live account
வைத்து verify பண்ணப்படல (angel_one.py-ல் get_option_chain() docstring-லும்
இதே caution இருக்கு). முதல் live run-ல் logs பார்த்து confirm பண்ணிக்கொள்ளவும்.
"""
from fastapi import APIRouter, Depends, HTTPException
import logging

from app.services.angel_one import AngelOneSession, AngelOneError
from app.api.deps import get_angel_session

router = APIRouter()
logger = logging.getLogger(__name__)

# எத்தனை % loss/profit-ல் AI "EXIT" சொல்லும் (cost-basis % — simple rule, investment advice இல்லை)
STOP_LOSS_PCT = -20.0
TARGET_PCT    = 30.0


def _compute_pnl(pos: dict) -> dict:
    """AngelOneSession.get_positions() ஒவ்வொரு row-க்கும் P&L, status கணக்கிடும்."""
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


def _ai_suggestion(p: dict) -> str:
    """
    Cost-basis % change வைத்து rule-based hold/exit suggestion.
    (Rule engine மட்டும் — investment advice இல்லை; UI-ல் disclaimer இருக்கு.)
    """
    invested = p["avg_price"] * p["quantity"]
    if invested <= 0:
        return "HOLD"
    change_pct = (p["pnl"] / invested) * 100

    if change_pct <= STOP_LOSS_PCT:
        return "EXIT — Stop-loss level அடைந்துவிட்டது"
    if change_pct >= TARGET_PCT:
        return "EXIT — Target அடைந்துவிட்டது, profit book செய்யலாம்"
    return "HOLD"


@router.get("/positions")
async def get_positions(angel: AngelOneSession = Depends(get_angel_session)):
    try:
        raw_positions = await angel.get_positions()
    except AngelOneError as e:
        logger.error(f"get_positions failed: {e}")
        raise HTTPException(502, detail=f"Positions fetch பண்ண முடியவில்லை: {e}")

    result = []
    total_pnl = 0.0
    for pos in raw_positions:
        enriched = _compute_pnl(pos)
        enriched["ai_suggestion"] = _ai_suggestion(enriched)
        enriched["stop_loss_hit"] = enriched["ai_suggestion"].startswith("EXIT — Stop-loss")
        total_pnl += enriched["pnl"]
        result.append(enriched)

    return {
        "positions": result,
        "total_pnl": round(total_pnl, 2),
        "count":     len(result),
        "disclaimer": "AI suggestion ஒரு rule-based hint மட்டும் — investment advice இல்லை.",
    }


@router.post("/positions/square-off/{symbol}")
async def square_off_position(symbol: str, angel: AngelOneSession = Depends(get_angel_session)):
    try:
        result = await angel.square_off_position(symbol)
    except AngelOneError as e:
        logger.error(f"Square off failed for {symbol}: {e}")
        raise HTTPException(502, detail=f"Square off ஆகவில்லை: {e}")

    return {"symbol": symbol, "result": result}
