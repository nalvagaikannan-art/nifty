"""
Market Intelligence routes — spec sections not covered by /api/market:
FII/DII, Sector Performance, Global Market, Economic Calendar.

All free/public data sources only (see data_fetcher.get_fii_dii,
data_fetcher.get_sector_performance, services/global_market.py,
services/economic_calendar.py for source details). No broker order
placement here or anywhere else in this app.
"""
from fastapi import APIRouter, Depends
from app.services.data_fetcher import DataFetcher
from app.services.global_market import global_market_service
from app.services.economic_calendar import economic_calendar_service
from app.api.deps import get_fetcher

router = APIRouter()


@router.get("/fii-dii")
async def fii_dii(fetcher: DataFetcher = Depends(get_fetcher)):
    """FII/DII cash-market net activity — NSE official public JSON."""
    return await fetcher.get_fii_dii()


@router.get("/sector-performance")
async def sector_performance(fetcher: DataFetcher = Depends(get_fetcher)):
    """NSE sector indices — top/weak sector, rotation, strength score."""
    return await fetcher.get_sector_performance()


@router.get("/global-markets")
async def global_markets():
    """Gift Nifty*, Dow/Nasdaq/S&P futures, Nikkei, Hang Seng, Shanghai,
    FTSE, DAX, CAC, Crude, Gold, Silver, Dollar Index, USDINR, US 10Y yield.

    *Gift Nifty currently has no free public data source — see
    services/global_market.py docstring — and reports as unavailable
    rather than an estimated/fake number.
    """
    return await global_market_service.get_snapshot()


@router.get("/economic-calendar")
async def economic_calendar():
    """NSE weekly/monthly expiry (computed) + this week's macro events
    (Fed, CPI, GDP, Employment, RBI/INR-tagged, etc.) from a free public feed.
    """
    return await economic_calendar_service.get_calendar()
