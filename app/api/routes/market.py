from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from app.services.data_fetcher import DataFetcher
from app.exceptions import MarketDataError
from app.api.deps import get_fetcher

router = APIRouter()

VALID_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}
VALID_INTERVALS = {"ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE", "THIRTY_MINUTE", "ONE_HOUR"}

@router.get("/spot/{symbol}")
async def get_spot(symbol: str, fetcher: DataFetcher = Depends(get_fetcher)):
    try:
        return await fetcher.get_spot(symbol)
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/candles/{symbol}")
async def get_candles(
    symbol: str,
    interval: str = Query("FIVE_MINUTE"),
    bars: int = Query(100, ge=10, le=300),
    fetcher: DataFetcher = Depends(get_fetcher),
):
    """Real intraday OHLCV series for charting (Live page price chart).
    Reuses the same Angel One/Zerodha intraday path multi-timeframe analysis
    already relies on (see MarketAnalyzer._safe_multi_timeframe) — this
    endpoint just exposes the raw series instead of collapsing it into
    indicators, since a chart needs the series itself.
    Returns {"available": false, "reason": ...} (HTTP 200, not an error)
    when no intraday-capable provider is configured — same "honest
    unavailable, never fabricated" convention used everywhere else in this
    app, so the frontend can show a clear message instead of a broken chart."""
    sym = symbol.upper()
    if sym not in VALID_SYMBOLS:
        raise HTTPException(400, detail=f"Unsupported symbol: {symbol}")
    iv = interval.upper()
    if iv not in VALID_INTERVALS:
        raise HTTPException(400, detail=f"Unsupported interval: {interval}")
    try:
        return await fetcher.get_intraday_ohlc(sym, interval=iv, bars=bars)
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/option-chain/{symbol}")
async def get_option_chain(symbol: str, expiry: Optional[str] = Query(None), fetcher: DataFetcher = Depends(get_fetcher)):
    try:
        return await fetcher.get_option_chain(symbol, expiry)
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/vix")
async def get_vix(fetcher: DataFetcher = Depends(get_fetcher)):
    return {"vix": await fetcher.get_volatility()}

@router.get("/breadth")
async def get_breadth(fetcher: DataFetcher = Depends(get_fetcher)):
    return await fetcher.get_market_breadth()
