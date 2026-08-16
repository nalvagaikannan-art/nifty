import asyncio
from fastapi import APIRouter, Depends
from app.services.market_analyzer import MarketAnalyzer
from app.services.history_service import save_market_snapshot
from app.api.deps import get_analyzer

router = APIRouter()

@router.get("/summary")
async def dashboard_summary(analyzer: MarketAnalyzer = Depends(get_analyzer)):
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY"]

    async def _one(sym):
        try:
            data = await analyzer.get_full_market_overview(sym)
            dec  = data.get("decision", {})
            await save_market_snapshot(data["spot"])
            return sym, {
                "price":       data["spot"]["price"],
                "change":      data["spot"]["change_percent"],
                "high":        data["spot"].get("high", 0),
                "low":         data["spot"].get("low", 0),
                "pcr":         data.get("pcr", 0),
                "vix":         data.get("vix", 0),
                "max_pain":    data.get("max_pain", 0),
                "market_open": data["spot"].get("market_open", False),
                "market_bias":         dec.get("market_bias", "Sideways"),
                "bullish_probability": dec.get("bullish_probability", 50),
                "bearish_probability": dec.get("bearish_probability", 50),
                "preferred_side":      dec.get("preferred_side", "NONE"),
                "bull_score":  dec.get("bull_score", 0),
                "bear_score":  dec.get("bear_score", 0),
                "confidence":  dec.get("confidence", 0),
                "forecast":    dec.get("forecast", "Neutral"),
                "risk":        dec.get("risk", "Medium"),
            }
        except Exception as e:
            return sym, {"error": str(e)}

    pairs = await asyncio.gather(*(_one(s) for s in symbols))
    return dict(pairs)
