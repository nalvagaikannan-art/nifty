"""
Angel One API Routes
/api/angel/status   — session status பார்க்க
/api/angel/login    — login செய்ய
/api/angel/logout   — logout செய்ய
/api/angel/ltp/{symbol} — live price
/api/angel/candles/{symbol} — OHLCV history
"""

from fastapi import APIRouter, HTTPException
from app.schemas import AngelLoginResponse, AngelLogoutResponse
from pydantic import BaseModel
from typing import Optional
from app.services.angel_one import angel_session, AngelOneAuthError, AngelOneError
from app.services.angel_live_feed import angel_live_feed
from app.schemas import (
    AngelStatusResponse,
    AngelLiveFeedResponse,
    AngelLtpResponse,
    AngelCandlesResponse,
    AngelOptionChainResponse,
)
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Status ─────────────────────────────────────────────────────────────────
@router.get("/status", response_model=AngelStatusResponse)
async def get_status():
    """Angel One session status + configured check."""
    return angel_session.get_status()


# ── Live WebSocket Feed Diagnostic ───────────────────────────────────────────
@router.get("/live-feed", response_model=AngelLiveFeedResponse)
async def get_live_feed_status():
    """Production Angel WebSocket connection and latest tick diagnostics."""
    return {
        "websocket": angel_live_feed.status(),
        "ticks": angel_live_feed.get_snapshot(),
    }


# ── Login ──────────────────────────────────────────────────────────────────
@router.post("/login", response_model=AngelLoginResponse)
async def login():
    """Angel One-ல் login செய்யும். TOTP automatic-ஆக generate ஆகும்."""
    try:
        result = await angel_session.login()
        return result
    except AngelOneAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except AngelOneError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Logout ─────────────────────────────────────────────────────────────────
@router.post("/logout", response_model=AngelLogoutResponse)
async def logout():
    """Session terminate செய்யும்."""
    return await angel_session.logout()


# ── Live LTP ───────────────────────────────────────────────────────────────
@router.get("/ltp/{symbol}", response_model=AngelLtpResponse)
async def get_ltp(symbol: str):
    """Angel One-இல் இருந்து live LTP."""
    if not angel_session.is_configured:
        raise HTTPException(
            status_code=503,
            detail="Angel One credentials configured இல்லை. Settings-ல் சேர்க்கவும்."
        )
    try:
        return await angel_session.get_ltp(symbol.upper())
    except AngelOneAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except AngelOneError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Candle Data ─────────────────────────────────────────────────────────────
@router.get("/candles/{symbol}", response_model=AngelCandlesResponse)
async def get_candles(
    symbol: str,
    interval: str = "ONE_DAY",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
):
    """
    Historical OHLCV candles.
    interval: ONE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE, ONE_HOUR, ONE_DAY
    """
    if not angel_session.is_configured:
        raise HTTPException(
            status_code=503,
            detail="Angel One credentials configured இல்லை."
        )
    try:
        data = await angel_session.get_candle_data(symbol.upper(), interval, from_date or "", to_date or "")
        return {"symbol": symbol.upper(), "interval": interval, "candles": data}
    except AngelOneError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Option Chain ────────────────────────────────────────────────────────────
@router.get("/option-chain/{symbol}", response_model=AngelOptionChainResponse)
async def get_option_chain(symbol: str, expiry: Optional[str] = None):
    """Angel One option chain data."""
    if not angel_session.is_configured:
        raise HTTPException(
            status_code=503,
            detail="Angel One credentials configured இல்லை."
        )
    try:
        data = await angel_session.get_option_chain(symbol.upper(), expiry)
        return {"symbol": symbol.upper(), "expiry": expiry, "data": data}
    except AngelOneError as e:
        raise HTTPException(status_code=502, detail=str(e))
