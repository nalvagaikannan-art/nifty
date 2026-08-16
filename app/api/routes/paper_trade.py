"""
Paper Trading API Routes — V2
==============================
POST /api/paper-trade/open
POST /api/paper-trade/close/{trade_id}
GET  /api/paper-trade/open
GET  /api/paper-trade/history
GET  /api/paper-trade/stats
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from app.services.paper_trading import (
    open_paper_trade,
    close_paper_trade,
    get_open_trades,
    get_trade_history,
    get_paper_trade_stats,
    get_daily_pnl,
)
import logging

router  = APIRouter()
logger  = logging.getLogger(__name__)


# ── Request schemas ────────────────────────────────────────────────────────

class OpenTradeRequest(BaseModel):
    symbol:            str          = "NIFTY"
    expiry:            Optional[str] = None
    strike:            float
    option_type:       str          = Field(..., pattern="^(CE|PE)$")
    side:              str          = "BUY"
    entry_price:       float        = Field(..., gt=0)
    stop_loss:         float        = Field(..., gt=0)
    target_1:          float        = Field(..., gt=0)
    target_2:          float        = Field(..., gt=0)
    target_3:          Optional[float] = None
    lots:              int          = Field(1, ge=1, le=10)
    quantity:          Optional[int] = None
    signal_strength:   Optional[int] = None
    confluence_score:  Optional[int] = None
    confluence_quality: Optional[str] = None
    market_regime:     Optional[str] = None
    trigger_level:     Optional[float] = None
    spot_at_entry:     Optional[float] = None
    rr_ratio:          Optional[float] = None
    vix_at_entry:      Optional[float] = None
    pcr_at_entry:      Optional[float] = None
    notes:             Optional[str] = None


class CloseTradeRequest(BaseModel):
    exit_price:  float  = Field(..., gt=0)
    exit_reason: str    = "MANUAL"   # MANUAL / STOP_LOSS / TARGET_1 / TARGET_2 / EXPIRED


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/open")
async def open_trade(req: OpenTradeRequest):
    """New paper trade open செய்கிறோம்"""
    try:
        data = req.model_dump()
        result = await open_paper_trade(data)
        return {"success": True, "trade": result}
    except Exception as e:
        logger.exception("Failed to open paper trade")
        raise HTTPException(500, detail=str(e))


@router.post("/close/{trade_id}")
async def close_trade(trade_id: str, req: CloseTradeRequest):
    """Paper trade close செய்கிறோம்"""
    result = await close_paper_trade(trade_id, req.exit_price, req.exit_reason)
    if "error" in result:
        raise HTTPException(404, detail=result["error"])
    return {"success": True, "trade": result}


@router.get("/open")
async def list_open_trades(symbol: Optional[str] = None):
    """Open positions list"""
    trades = await get_open_trades(symbol)
    return {
        "open_trades": trades,
        "count":       len(trades),
    }


@router.get("/history")
async def trade_history(symbol: Optional[str] = None, limit: int = 50):
    """Trade history (closed trades)"""
    trades = await get_trade_history(symbol, limit)
    return {
        "history": trades,
        "count":   len(trades),
    }


@router.get("/stats")
async def paper_stats(symbol: Optional[str] = None):
    """
    P&L stats + signal strength calibration.
    100+ trades பிறகு win probability estimates கிடைக்கும்.
    """
    stats     = await get_paper_trade_stats(symbol)
    daily_pnl = await get_daily_pnl()
    stats["daily_pnl"] = round(daily_pnl, 2)
    return stats


@router.get("/daily-pnl")
async def daily_pnl_endpoint():
    pnl = await get_daily_pnl()
    return {"daily_pnl": round(pnl, 2)}
