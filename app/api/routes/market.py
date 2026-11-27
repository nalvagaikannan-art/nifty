from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from app.services.data_fetcher import DataFetcher
from app.exceptions import MarketDataError
from app.api.deps import get_fetcher, get_angel_session
from app.services.angel_one import AngelOneSession
from app.schemas import (MarketOptionChainResponse, MarketSpotResponse, MarketCandlesResponse, MarketVixResponse, MarketBreadthResponse)

router = APIRouter()

VALID_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}
VALID_INTERVALS = {"ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE", "THIRTY_MINUTE", "ONE_HOUR"}

@router.get("/fno-stock-scan")
async def fno_stock_scan(angel: AngelOneSession = Depends(get_angel_session)):
    """Cached Angel One batch quote scan for the verified NSE F&O stock universe.

    This is a market-move shortlist, not an options trade recommendation.
    """
    try:
        return await angel.get_fno_stock_market_scan()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"F&O stock scan unavailable: {e}")


@router.get("/symbols")
async def get_market_symbols(fetcher: DataFetcher = Depends(get_fetcher)):
    """Current supported indices and broker-master-verified NSE F&O stocks."""
    return await fetcher.get_market_symbol_universe()


@router.get("/spot/{symbol}", response_model=MarketSpotResponse)
async def get_spot(symbol: str, fetcher: DataFetcher = Depends(get_fetcher)):
    sym = symbol.strip().upper()
    if not await fetcher.is_supported_market_symbol(sym):
        raise HTTPException(400, detail=f"Unsupported or unavailable NSE F&O symbol: {symbol}")
    try:
        return await fetcher.get_spot(sym)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/candles/{symbol}", response_model=MarketCandlesResponse)
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
    sym = symbol.strip().upper()
    if not await fetcher.is_supported_market_symbol(sym):
        raise HTTPException(400, detail=f"Unsupported or unavailable NSE F&O symbol: {symbol}")
    iv = interval.upper()
    if iv not in VALID_INTERVALS:
        raise HTTPException(400, detail=f"Unsupported interval: {interval}")
    try:
        return await fetcher.get_intraday_ohlc(sym, interval=iv, bars=bars)
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/option-chain/{symbol}", response_model=MarketOptionChainResponse)
async def get_option_chain(symbol: str, expiry: Optional[str] = Query(None), fetcher: DataFetcher = Depends(get_fetcher)):
    sym = symbol.strip().upper()
    if not await fetcher.is_supported_market_symbol(sym):
        raise HTTPException(status_code=400, detail=f"Unsupported or unavailable NSE F&O symbol: {symbol}")
    try:
        return await fetcher.get_option_chain(sym, expiry)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/mcx/contracts")
async def get_mcx_contracts(
    commodity: str = Query("GOLDPETAL", min_length=2, max_length=30),
    angel: AngelOneSession = Depends(get_angel_session),
):
    """Verified Angel One MCX FUTCOM contracts; isolated from NSE/BSE indices."""
    key = commodity.strip().upper()
    if key != "GOLDPETAL":
        raise HTTPException(status_code=400, detail="Only the GOLDPETAL MCX pilot is enabled")
    try:
        contracts = await angel.get_mcx_futures_contracts(key)
        return {"exchange": "MCX", "commodity": key, "available": bool(contracts), "contracts": contracts}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCX contract master unavailable: {exc}")


@router.get("/mcx/quote/{commodity}")
async def get_mcx_quote(
    commodity: str,
    angel: AngelOneSession = Depends(get_angel_session),
):
    """Angel One MCX FUTCOM LTP. No NSE, Yahoo or Zerodha substitution."""
    key = commodity.strip().upper()
    if key != "GOLDPETAL":
        raise HTTPException(status_code=400, detail="Only the GOLDPETAL MCX pilot is enabled")
    try:
        quote = await angel.get_mcx_futures_ltp(key)
        if not quote:
            return {"available": False, "exchange": "MCX", "commodity": key, "reason": "Angel One MCX quote unavailable"}
        return {"available": True, **quote}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCX quote unavailable: {exc}")


@router.get("/mcx/candles/{commodity}")
async def get_mcx_candles(
    commodity: str,
    interval: str = Query("FIVE_MINUTE"),
    bars: int = Query(100, ge=10, le=300),
    angel: AngelOneSession = Depends(get_angel_session),
):
    """Historical candles from the resolved MCX FUTCOM contract only."""
    key = commodity.strip().upper()
    if key != "GOLDPETAL":
        raise HTTPException(status_code=400, detail="Only the GOLDPETAL MCX pilot is enabled")
    iv = interval.upper()
    if iv not in VALID_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Unsupported interval: {interval}")
    try:
        candles = await angel.get_mcx_futures_candle_data(key, iv)
        return {"available": bool(candles), "exchange": "MCX", "commodity": key, "interval": iv, "candles": candles[-bars:]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCX candles unavailable: {exc}")


@router.get("/vix", response_model=MarketVixResponse)
async def get_vix(fetcher: DataFetcher = Depends(get_fetcher)):
    return {"vix": await fetcher.get_volatility()}

@router.get("/breadth", response_model=MarketBreadthResponse)
async def get_breadth(fetcher: DataFetcher = Depends(get_fetcher)):
    return await fetcher.get_market_breadth()
