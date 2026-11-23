"""
NIFTY-PRO — paper-only order ledger
=====================================
This build records simulated paper fills only. Live broker execution is
permanently disabled in code: environment flags and PINs cannot enable it.
Angel One order-routing helpers fail closed before opening a broker session.
"""
import asyncio
import logging
import os
import uuid
from typing import Dict, List, Optional

from sqlalchemy import Column, DateTime, Float, Integer, JSON, String, Text, func, select

from app.config import settings
from app.database import AsyncSessionLocal, Base
from app.services.contract_specs import resolve_lot_size
from app.utils.helpers import now_ist

logger = logging.getLogger(__name__)

_runtime_kill = {"on": False}


class OrderRejected(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


class ProOrder(Base):
    __tablename__ = "pro_orders"
    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex[:10])
    created_at = Column(DateTime, server_default=func.now())
    trade_date = Column(String, index=True)
    mode = Column(String)                  # paper | live
    signal_id = Column(String, nullable=True)
    symbol = Column(String)
    tradingsymbol = Column(String, nullable=True)
    exchange = Column(String, nullable=True)
    side = Column(String)
    role = Column(String, nullable=True)   # CORE | HEDGE
    quantity = Column(Integer)
    price = Column(Float, nullable=True)
    status = Column(String)                # PAPER_FILLED | SENT | REJECTED | ERROR
    broker_order_id = Column(String, nullable=True)
    detail = Column(JSON, nullable=True)
    note = Column(Text, nullable=True)


def kill_switch_on() -> bool:
    return _runtime_kill["on"] or os.path.exists(settings.kill_switch_file)


def set_kill_switch(on: bool) -> bool:
    _runtime_kill["on"] = bool(on)
    try:
        os.makedirs(os.path.dirname(settings.kill_switch_file) or ".", exist_ok=True)
        if on:
            open(settings.kill_switch_file, "w").write(now_ist().isoformat())
        elif os.path.exists(settings.kill_switch_file):
            os.remove(settings.kill_switch_file)
    except OSError as e:
        logger.warning("kill switch file op failed: %s", e)
    return kill_switch_on()


def status() -> Dict:
    # This build is intentionally paper-only. Environment flags must never
    # advertise or enable broker execution.
    return {"mode": "paper", "configured_mode": settings.trading_mode,
            "live_enabled": False, "pin_configured": False,
            "kill_switch": kill_switch_on(), "max_lots_per_order": settings.max_lots_per_order,
            "max_orders_per_day": settings.max_orders_per_day, "allow_naked_sell": settings.allow_naked_sell}


def _is_live(pin: Optional[str]) -> bool:
    """Reject live-mode configuration; otherwise use the isolated paper ledger.

    A PIN alone never enables live trading. Broker routing remains blocked by
    _angel_place even if this guard is bypassed accidentally.
    """
    if str(settings.trading_mode).lower() == "live" or bool(settings.live_trading_enabled):
        raise OrderRejected("PAPER_ONLY", "இந்த build paper-only. Broker live order execution நிரந்தரமாக முடக்கப்பட்டுள்ளது.")
    return False


def build_orders(signal: Dict, lots: int, include_hedge: bool = True) -> List[Dict]:
    """Legs -> ordered order list: HEDGE (BUY) first, then CORE (SELL)."""
    lot = signal.get("lot_size") or resolve_lot_size(signal.get("symbol", ""))
    if not lot:
        raise OrderRejected("NO_LOT", "lot size தெரியவில்லை.")
    qty = lot * lots
    hedges = signal.get("hedge_legs", []) if include_hedge else []
    orders = [dict(l, quantity=qty) for l in hedges] + [dict(l, quantity=qty) for l in signal.get("legs", [])]
    return orders


def validate(signal: Dict, lots: int, include_hedge: bool, orders_today: int) -> List[Dict]:
    if kill_switch_on():
        raise OrderRejected("KILL_SWITCH", "Kill switch ON — எல்லா ஆர்டர்களும் நிறுத்தப்பட்டுள்ளன.")
    if not signal.get("executable", True):
        raise OrderRejected("ADVISORY_ONLY", "இந்த சிக்னல் advisory மட்டும்.")
    if lots < 1 or lots > settings.max_lots_per_order:
        raise OrderRejected("LOT_LIMIT", f"lots 1..{settings.max_lots_per_order} மட்டும் (MAX_LOTS_PER_ORDER).")
    orders = build_orders(signal, lots, include_hedge)
    lot = signal.get("lot_size") or resolve_lot_size(signal.get("symbol", ""))
    if any(o["quantity"] % lot for o in orders):
        raise OrderRejected("LOT_MULTIPLE", f"quantity {lot}-ன் மடங்கு அல்ல.")
    if orders_today + len(orders) > settings.max_orders_per_day:
        raise OrderRejected("DAILY_LIMIT", f"தினசரி ஆர்டர் வரம்பு ({settings.max_orders_per_day}) தாண்டும்.")
    sells = [o for o in orders if o["side"] == "SELL"]
    hedged = sum(1 for o in orders if o["side"] == "BUY") >= len(sells) and bool(sells)
    if sells and not hedged and not settings.allow_naked_sell:
        raise OrderRejected("NAKED_SELL", "Hedge (wings) இல்லாத naked SELL தடை (ALLOW_NAKED_SELL=false).")
    if any(o["price"] <= 0 for o in orders):
        raise OrderRejected("BAD_PRICE", "ஒரு leg-ன் விலை 0 — ஆர்டர் இல்லை.")
    return orders


async def _orders_today() -> int:
    day = now_ist().date().isoformat()
    async with AsyncSessionLocal() as s:
        return int((await s.execute(select(func.count()).select_from(ProOrder).where(
            ProOrder.trade_date == day, ProOrder.status.in_(["PAPER_FILLED", "SENT"])))).scalar() or 0)


async def _log(**kw) -> str:
    async with AsyncSessionLocal() as s:
        row = ProOrder(trade_date=now_ist().date().isoformat(), **kw)
        s.add(row)
        await s.commit()
        return row.id


async def _free_cash() -> float:
    from app.services.angel_one import angel_session
    await angel_session.ensure_session()
    rms = await asyncio.to_thread(angel_session._obj.rmsLimit)
    return float(((rms or {}).get("data") or {}).get("availablecash") or 0)


async def _angel_place(o: Dict, symbol: str, spot: float = 0.0) -> Dict:
    # Defense in depth: even accidental direct calls cannot reach Angel One.
    raise OrderRejected("PAPER_ONLY", "Broker live order execution is disabled in this paper-only build.")
    from app.services.angel_one import angel_session, _normalize_expiry
    await angel_session.ensure_session()
    meta = await angel_session.resolve_option_tokens(symbol, o["expiry"], strikes_each_side=None, spot_price=spot or None)
    hit = next((m for m in meta if float(m["strike"]) == float(o["strike"]) and m["optionType"] == o["option_type"]), None)
    if not hit:
        raise OrderRejected("NO_TOKEN", f"{symbol} {o['strike']}{o['option_type']} token கிடைக்கவில்லை.")
    # resolve_option_tokens silently falls back to the NEAREST expiry when the requested
    # one is not found — never let that turn into an order on the wrong expiry.
    if _normalize_expiry(hit.get("expiry")) != _normalize_expiry(o["expiry"]):
        raise OrderRejected("EXPIRY_MISMATCH", f"கேட்ட expiry {o['expiry']} ≠ கிடைத்தது {hit.get('expiry')} — ஆர்டர் இல்லை.")
    exch = angel_session.DERIVATIVE_EXCHANGE_MAP.get(symbol.upper(), "NFO")
    params = {"variety": "NORMAL", "tradingsymbol": hit["tradingsymbol"], "symboltoken": hit["token"],
              "transactiontype": o["side"], "exchange": exch, "ordertype": "LIMIT",
              "producttype": settings.order_product_type, "duration": "DAY",
              "price": str(round(float(o["price"]), 2)), "squareoff": "0", "stoploss": "0",
              "quantity": str(o["quantity"])}
    resp = await asyncio.to_thread(angel_session._obj.placeOrder, params)
    return {"tradingsymbol": hit["tradingsymbol"], "exchange": exch, "broker_order_id": str(resp)}


async def execute_signal(signal: Dict, lots: int = 1, include_hedge: bool = True, pin: Optional[str] = None) -> Dict:
    # Preserve the emergency stop as the first gate, then reject all live
    # execution before DB access, token lookup, margin lookup, or broker calls.
    if kill_switch_on():
        raise OrderRejected("KILL_SWITCH", "Kill switch ON — எல்லா ஆர்டர்களும் நிறுத்தப்பட்டுள்ளன.")
    live = _is_live(pin)
    today = await _orders_today()
    orders = validate(signal, lots, include_hedge, today)
    sym, sig_id = signal.get("symbol", ""), signal.get("id")
    results, mode = [], "live" if live else "paper"
    if live:
        sells = [o for o in orders if o["side"] == "SELL"]
        need = settings.min_free_cash_per_sell_lot * lots if sells else 0
        if need:
            cash = await _free_cash()
            if cash < need:
                raise OrderRejected("LOW_CASH", f"Free cash ₹{cash:,.0f} < தேவை ₹{need:,.0f}.")
    for o in orders:                                   # hedges first (list order)
        base = dict(mode=mode, signal_id=sig_id, symbol=sym, side=o["side"], role=o.get("role"),
                    quantity=o["quantity"], price=o["price"], exchange=None,
                    tradingsymbol=f"{sym} {o['strike']:.0f}{o['option_type']} {o['expiry']}")
        if not live:
            oid = await _log(status="PAPER_FILLED", note="paper fill at signal price", **base)
            results.append({"order_id": oid, "status": "PAPER_FILLED", **{k: o[k] for k in ("side", "strike", "option_type", "price", "quantity")}})
            continue
        try:
            r = await _angel_place(o, sym, float(signal.get('spot') or 0))
            oid = await _log(status="SENT", broker_order_id=r["broker_order_id"], detail=r,
                             **{**base, "tradingsymbol": r["tradingsymbol"], "exchange": r["exchange"]})
            results.append({"order_id": oid, "status": "SENT", "broker_order_id": r["broker_order_id"], "side": o["side"], "strike": o["strike"], "option_type": o["option_type"]})
        except Exception as e:
            oid = await _log(status="REJECTED" if isinstance(e, OrderRejected) else "ERROR", note=str(e)[:500], **base)
            results.append({"order_id": oid, "status": "ERROR", "error": str(e)[:300]})
            logger.error("Live order failed on leg %s — STOPPING remaining legs: %s", o, e)
            return {"mode": mode, "complete": False, "legs": results,
                    "warning": "ஒரு leg தோல்வி — மீதி legs அனுப்பப்படவில்லை. broker-ல் open positions-ஐ கைமுறையாகச் சரிபார்க்கவும்."}
    return {"mode": mode, "complete": True, "legs": results}


async def list_orders(limit: int = 50) -> List[Dict]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(ProOrder).order_by(ProOrder.created_at.desc()).limit(limit))).scalars().all()
    return [{"id": r.id, "time": str(r.created_at), "mode": r.mode, "signal_id": r.signal_id, "leg": r.tradingsymbol,
             "side": r.side, "role": r.role, "qty": r.quantity, "price": r.price, "status": r.status,
             "broker_order_id": r.broker_order_id, "note": r.note} for r in rows]
