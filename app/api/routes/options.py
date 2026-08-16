from fastapi import APIRouter, Depends
from app.services.option_analyzer import OptionAnalyzer
from app.services.data_fetcher import DataFetcher
from app.api.deps import get_fetcher

router = APIRouter()

@router.get("/pcr/{symbol}")
async def get_pcr(symbol: str, fetcher: DataFetcher = Depends(get_fetcher)):
    chain = await fetcher.get_option_chain(symbol)
    analyzer = OptionAnalyzer()
    df = analyzer.process_option_chain(chain)
    pcr = analyzer.compute_pcr(df)
    return {"symbol": symbol, "pcr": pcr}

@router.get("/maxpain/{symbol}")
async def get_maxpain(symbol: str, fetcher: DataFetcher = Depends(get_fetcher)):
    chain = await fetcher.get_option_chain(symbol)
    analyzer = OptionAnalyzer()
    df = analyzer.process_option_chain(chain)
    maxpain = analyzer.compute_max_pain(df)
    return {"symbol": symbol, "max_pain": maxpain}

@router.get("/chain/{symbol}")
async def get_option_chain(
    symbol: str,
    expiry: str = None,
    fetcher: DataFetcher = Depends(get_fetcher),
):
    chain = await fetcher.get_option_chain(symbol, expiry=expiry)
    analyzer = OptionAnalyzer()
    df = analyzer.process_option_chain(chain)
    underlying = chain.get("underlying_price", 0)
    pcr        = analyzer.compute_pcr(df)
    max_pain   = analyzer.compute_max_pain(df)
    oi_sum     = analyzer.oi_summary(df, underlying=underlying)
    candidates = analyzer.pick_candidates(df, underlying)
    return {
        "symbol":           chain.get("symbol", symbol),
        "expiry":           chain.get("expiry", ""),
        "all_expiries":     chain.get("all_expiries", []),
        "underlying_price": underlying,
        "pcr":              pcr,
        "max_pain":         max_pain,
        "oi_summary":       oi_sum,
        "candidates":       candidates,
        "data_source":      chain.get("data_source", ""),
        "data":             chain.get("data", []),
    }

@router.get("/{symbol}")
async def get_option_chain_alias(
    symbol: str,
    expiry: str = None,
    fetcher: DataFetcher = Depends(get_fetcher),
):
    """Alias for /chain/{symbol}, flattened for the terminal/beginner
    dashboards (which read atm_call_oi/atm_put_oi/iv_skew at the top
    level, not nested under oi_summary). Kept as a thin wrapper so
    /chain/{symbol} stays the canonical, unchanged endpoint for existing
    consumers — no duplicate chain fetch here, just reshaping.
    """
    data = await get_option_chain(symbol, expiry, fetcher)
    oi_sum = data.get("oi_summary", {})
    data["atm_call_oi"] = oi_sum.get("atm_call_oi")
    data["atm_put_oi"]  = oi_sum.get("atm_put_oi")
    data["iv_skew"]     = oi_sum.get("iv_skew")
    return data
