"""
NIFTY-PRO — MCX commodity feed + impact meter (blueprint §3-ஆ, §4-இ, §6)
=========================================================================
Source order:
  1. Zerodha Kite Connect (MCX futures: GOLD / SILVER / CRUDEOIL) — live LTP +
     15-minute candles -> RSI(14).   Needs KITE_API_KEY + KITE_ACCESS_TOKEN
     and `pip install kiteconnect`.
  2. Fallback: Yahoo global snapshot already in this app (global_market.py) —
     % change of WTI crude / gold / silver / USDINR (no MCX price, no RSI).

⚠ Kite path is written to Kite Connect v3 docs but NOT exercised against a live
  account here (same status as the original zerodha.py). Check logs on first run.

Impact meter: nifty_impact_pct = crude_change_pct x CRUDE_NIFTY_BETA (default -0.4).
That beta is a HEURISTIC from the blueprint's example (crude +2% -> Nifty -0.8%),
not a fitted statistic — tune CRUDE_NIFTY_BETA from your own data.
"""
import asyncio
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from app.config import settings
from app.utils.helpers import now_ist, safe_float

logger = logging.getLogger(__name__)

MCX = {"GOLD": "gold", "SILVER": "silver", "CRUDEOIL": "crude_oil"}   # MCX name -> global_market key


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder RSI. None when not enough data."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def commodity_impact(crude_change_pct: Optional[float], beta: Optional[float] = None) -> Optional[Dict]:
    if crude_change_pct is None:
        return None
    b = settings.crude_nifty_beta if beta is None else beta
    return {"crude_change_pct": round(crude_change_pct, 2), "beta": b,
            "nifty_impact_pct": round(crude_change_pct * b, 2), "basis": "heuristic_beta_not_fitted"}


def commodity_ideas(snap: Dict, nifty_change_pct: Optional[float] = None) -> List[Dict]:
    """Blueprint §4-இ rules. Ideas only — human decides."""
    ideas: List[Dict] = []
    c, g, s = snap.get("CRUDEOIL", {}), snap.get("GOLD", {}), snap.get("SILVER", {})
    usd = snap.get("USDINR", {}).get("change_pct")
    if c.get("change_pct") is not None and usd is not None:
        if c["change_pct"] > 0.5 and usd > 0 and (c.get("rsi15") is None or c["rsi15"] > 55):
            ideas.append({"symbol": "CRUDEOIL", "bias": "LONG",
                          "reason": f"Crude {c['change_pct']:+.2f}% & USDINR {usd:+.2f}% (ரூபாய் பலவீனம்)"
                                    + (f", RSI15 {c['rsi15']}" if c.get("rsi15") is not None else "")})
    if nifty_change_pct is not None and nifty_change_pct < -0.5:
        for name, d in (("GOLD", g), ("SILVER", s)):
            if d.get("change_pct") is not None and d["change_pct"] > 0 and (d.get("rsi15") is None or d["rsi15"] > 50):
                ideas.append({"symbol": name, "bias": "LONG (hedge)",
                              "reason": f"Nifty {nifty_change_pct:+.2f}% பலவீனம்; {name} {d['change_pct']:+.2f}%"})
    for i in ideas:
        i["executable"] = False   # ideas only — no MCX order placement in this build
    return ideas


# ── Kite (MCX) ───────────────────────────────────────────────────────────
class McxKite:
    def __init__(self):
        self._contracts: Dict[str, Dict] = {}
        self._contracts_day = None

    @property
    def available(self) -> bool:
        from app.services.zerodha import KiteSession  # noqa
        return bool(settings.kite_api_key and settings.kite_access_token)

    def _client(self):
        from app.services.zerodha import kite_session
        return kite_session._client()

    def _resolve(self) -> Dict[str, Dict]:
        today = now_ist().date()
        if self._contracts and self._contracts_day == today:
            return self._contracts
        rows = self._client().instruments("MCX")
        out = {}
        for name in MCX:
            futs = [r for r in rows if r.get("name") == name and r.get("instrument_type") == "FUT"
                    and r.get("expiry") and r["expiry"] >= today]
            if futs:
                out[name] = min(futs, key=lambda r: r["expiry"])
        self._contracts, self._contracts_day = out, today
        return out

    def snapshot_blocking(self) -> Dict[str, Dict]:
        kite = self._client()
        contracts = self._resolve()
        keys = [f"MCX:{c['tradingsymbol']}" for c in contracts.values()]
        quotes = kite.quote(keys) if keys else {}
        res = {}
        now = now_ist().replace(tzinfo=None)
        for name, c in contracts.items():
            q = quotes.get(f"MCX:{c['tradingsymbol']}", {})
            last = safe_float(q.get("last_price"))
            prev = safe_float((q.get("ohlc") or {}).get("close"))
            candles = kite.historical_data(c["instrument_token"], now - timedelta(days=5), now, "15minute")
            closes = [safe_float(x.get("close")) for x in candles]
            res[name] = {"source": "kite_mcx", "tradingsymbol": c["tradingsymbol"], "token": c["instrument_token"],
                         "expiry": str(c["expiry"]), "lot_size": c.get("lot_size"), "ltp": last,
                         "change_pct": round((last - prev) / prev * 100, 2) if prev else None,
                         "rsi15": rsi(closes)}
        return res


mcx_kite = McxKite()


async def get_commodity_snapshot() -> Dict:
    from app.services.global_market import global_market_service
    snap: Dict = {"_kite_live": False}
    glob = {}
    try:
        glob = (await global_market_service.get_snapshot()).get("instruments", {})
    except Exception as e:
        logger.warning("global snapshot failed for commodities: %s", type(e).__name__)
    if mcx_kite.available:
        try:
            snap.update(await asyncio.to_thread(mcx_kite.snapshot_blocking))
            snap["_kite_live"] = True
        except Exception as e:
            logger.warning("Kite MCX snapshot failed: %s", e)
            snap["_kite_error"] = f"{type(e).__name__}: {e}"
    for name, key in MCX.items():
        if name not in snap:
            g = glob.get(key, {})
            snap[name] = {"source": g.get("source", "unavailable"), "ltp": g.get("price"),
                          "change_pct": g.get("change_percent"), "rsi15": None,
                          "note": "MCX விலை அல்ல — global (USD) futures % மாற்றம்"}
    u = glob.get("usdinr", {})
    snap["USDINR"] = {"source": u.get("source", "unavailable"), "ltp": u.get("price"),
                      "change_pct": u.get("change_percent")}
    return snap
