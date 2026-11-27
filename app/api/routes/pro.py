"""
NIFTY-PRO routes  (/api/pro/*)
==============================
 GET  /status                     mode, limits, kill switch
 GET  /overview/{symbol}          chain metrics + IV Rank + SELL suggestions
 GET  /commodities                MCX (Kite) / global fallback, RSI, impact meter, ideas
 GET  /news/{topic}               headline sentiment (needs NEWSAPI_KEY)
 POST /orders/preview             validate a signal (no order, no log)
 POST /orders/execute             PAPER-ONLY simulated ledger; broker routing is disabled
 POST /kill-switch                {"on": true|false}
 GET  /orders                     paper order ledger

SECURITY: execute/preview NEVER trust prices/legs from the client. The client sends
only signal_id + lots; the server rebuilds the signals from the live chain and
records simulated paper fills only. Live broker execution is blocked in
app.services.order_executor regardless of environment flags or PIN configuration.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_fetcher
from app.config import settings
from app.services import commodity_feed, news_sentiment, order_executor
from app.services.data_fetcher import DataFetcher
from app.services.iv_history import get_iv_values, record_iv
from app.services.option_analyzer import OptionAnalyzer
from app.services.pro_metrics import chain_metrics, iv_rank_percentile
from app.services.sell_signals import generate_sell_signals
from app.utils.helpers import days_to_expiry

logger = logging.getLogger(__name__)
router = APIRouter()
SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}


class ExecuteBody(BaseModel):
    symbol: str
    signal_id: str
    expiry: Optional[str] = None
    lots: int = Field(1, ge=1)
    include_hedge: bool = True


async def build_overview(symbol: str, expiry: Optional[str], fetcher: DataFetcher,
                         adx: Optional[float] = None, holdings_lots: int = 0) -> dict:
    sym = symbol.upper()
    if sym not in SYMBOLS:
        raise HTTPException(400, f"symbol {sorted(SYMBOLS)} மட்டும்")
    chain = await fetcher.get_option_chain(sym, expiry=expiry, strikes_each_side=20)
    df = OptionAnalyzer().process_option_chain(chain)
    spot = float(chain.get("underlying_price") or 0)
    exp = chain.get("expiry") or expiry or ""
    try:
        vix_info = await fetcher.get_volatility_info()
        vix = float(vix_info.get("value") or 0) if vix_info.get("available") else 0.0
    except Exception:
        vix = 0.0
    m = chain_metrics(df, spot)
    if m.get("ok"):
        await record_iv(sym, m["atm_iv"], vix=vix, spot=spot)
    iv = iv_rank_percentile(m.get("atm_iv", 0), await get_iv_values(sym), settings.iv_rank_min_samples)
    res = generate_sell_signals({"symbol": sym, "spot": spot, "df": df, "expiry": exp, "dte": days_to_expiry(exp),
                                 "vix": vix, "iv": iv, "metrics": m, "adx": adx, "chain": chain,
                                 "holdings_lots": holdings_lots})
    res.update({"spot": spot, "data_source": chain.get("data_source"), "metrics": m, "iv": iv})
    return res


def _find(overview: dict, signal_id: str) -> dict:
    sig = next((s for s in overview["signals"] if s["id"] == signal_id), None)
    if not sig:
        raise HTTPException(409, "சிக்னல் இப்போது இல்லை/மாறிவிட்டது (சந்தை நகர்ந்தது) — refresh செய்து மீண்டும் முயற்சிக்கவும்.")
    return sig


@router.get("/status")
async def pro_status():
    return order_executor.status()


@router.get("/overview/{symbol}")
async def overview(symbol: str, expiry: Optional[str] = None, adx: Optional[float] = None,
                   holdings_lots: int = 0, fetcher: DataFetcher = Depends(get_fetcher)):
    return await build_overview(symbol, expiry, fetcher, adx=adx, holdings_lots=holdings_lots)


@router.get("/commodities")
async def commodities(nifty_change_pct: Optional[float] = None, fetcher: DataFetcher = Depends(get_fetcher)):
    snap = await commodity_feed.get_commodity_snapshot()
    if nifty_change_pct is None:
        try:
            nifty_change_pct = float((await fetcher.get_spot("NIFTY")).get("change_percent"))
        except Exception:
            nifty_change_pct = None
    return {"snapshot": snap,
            "impact": commodity_feed.commodity_impact(snap.get("CRUDEOIL", {}).get("change_pct")),
            "ideas": commodity_feed.commodity_ideas(snap, nifty_change_pct),
            "nifty_change_pct": nifty_change_pct}


@router.get("/news/{topic}")
async def news(topic: str):
    return await news_sentiment.get_sentiment(topic)


@router.post("/orders/preview")
async def preview(body: ExecuteBody, x_order_pin: Optional[str] = Header(None),
                  fetcher: DataFetcher = Depends(get_fetcher)):
    sig = _find(await build_overview(body.symbol, body.expiry, fetcher), body.signal_id)
    try:
        today = await order_executor._orders_today()
        orders = order_executor.validate(sig, body.lots, body.include_hedge, today)
    except order_executor.OrderRejected as e:
        raise HTTPException(422, {"code": e.code, "message": e.msg})
    return {"mode": order_executor.status()["mode"], "signal": sig,
            "orders": orders, "note": "முன்னோட்டம் மட்டும் — ஆர்டர் அனுப்பப்படவில்லை."}


@router.post("/orders/execute")
async def execute(body: ExecuteBody, x_order_pin: Optional[str] = Header(None),
                  fetcher: DataFetcher = Depends(get_fetcher)):
    sig = _find(await build_overview(body.symbol, body.expiry, fetcher), body.signal_id)
    try:
        return await order_executor.execute_signal(sig, body.lots, body.include_hedge, pin=x_order_pin)
    except order_executor.OrderRejected as e:
        raise HTTPException(422, {"code": e.code, "message": e.msg})


class KillBody(BaseModel):
    on: bool


@router.post("/kill-switch")
async def kill(body: KillBody, x_order_pin: Optional[str] = Header(None)):
    # Turning it ON is always allowed (fail-safe). Turning it OFF needs the PIN.
    if not body.on and (not settings.order_pin or x_order_pin != settings.order_pin):
        raise HTTPException(403, "Kill switch-ஐ OFF செய்ய சரியான X-Order-Pin தேவை.")
    return {"kill_switch": order_executor.set_kill_switch(body.on)}


@router.get("/orders")
async def orders(limit: int = 50):
    return {"orders": await order_executor.list_orders(min(max(limit, 1), 200))}
