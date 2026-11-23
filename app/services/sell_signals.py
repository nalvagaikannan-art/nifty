"""
NIFTY-PRO — SELL-suggestion engine (blueprint §4)
==================================================
Turns chain metrics + IV Rank into premium-selling *suggestions*:

  1. SHORT_STRADDLE / SHORT_STRANGLE  — IV Rank > 70 and range-bound market
  2. IRON_CONDOR                      — range-bound and IV Rank >= 30 (defined risk)
  3. COVERED_CALL                     — advisory only, needs stock/ETF holdings

Design rules (deliberately conservative):
  * No actionable signal while IV history is too short (rank not reliable).
  * Strikes sit at ~1σ (1.25 x ATM straddle) and are pushed BEYOND the OI wall
    (max call OI / max put OI) when the wall is further out.
  * Naked legs (straddle/strangle without wings) are flagged `defined_risk=False`;
    each short signal ships with `hedge_legs` (long wings) so it can be traded
    as iron butterfly / iron condor. The order executor places hedges FIRST.
  * Expiry day (DTE=0): only defined-risk structures, and "close by 3 PM".
  * Every number is derived from the live chain — nothing is invented.
"""
import math
from typing import Dict, List, Optional

import pandas as pd

from app.config import settings
from app.services.contract_specs import resolve_lot_size


def _row(df: pd.DataFrame, strike: float) -> Optional[pd.Series]:
    m = df[df["strike"] == strike]
    return m.iloc[0] if not m.empty else None


def _nearest_strike(df: pd.DataFrame, target: float) -> float:
    return float(df.loc[(df["strike"] - target).abs().idxmin(), "strike"])


def _round_up(x: float, step: float) -> float:
    return math.ceil(x / step) * step


def _round_down(x: float, step: float) -> float:
    return math.floor(x / step) * step


def _leg(df, strike, opt, side, role, expiry):
    r = _row(df, strike)
    if r is None:
        return None
    k = "ce" if opt == "CE" else "pe"
    price = float(r.get(f"{k}_mid", 0) or 0) or float(r.get(f"{k}_ltp", 0) or 0)
    if price <= 0:
        return None
    return {"strike": strike, "option_type": opt, "side": side, "role": role,
            "expiry": expiry, "price": round(price, 2),
            "spread_pct": round(float(r.get(f"{k}_spread_pct", 0) or 0), 2)}


def _range_bound(m: Dict, spot: float, adx: Optional[float]) -> Dict:
    """ADX<20 when ADX is supplied; else a transparent chain-based proxy."""
    if adx is not None and adx > 0:
        return {"ok": adx < 20, "basis": f"ADX {adx:.1f} {'< 20' if adx < 20 else '>= 20'}"}
    near_pain = m.get("max_pain") and abs(spot - m["max_pain"]) / spot <= 0.004
    neutral = 0.8 <= (m.get("pcr") or 0) <= 1.2
    return {"ok": bool(near_pain and neutral),
            "basis": f"ADX இல்லை → proxy: spot-max pain ≤0.4% ({'ஆம்' if near_pain else 'இல்லை'}), PCR 0.8–1.2 ({'ஆம்' if neutral else 'இல்லை'})"}


def _confidence(rank, rb_ok, pcr_neutral, vix, dte) -> int:
    score = 40 * min((rank or 0) / 100, 1) + (25 if rb_ok else 0) + (15 if pcr_neutral else 0)
    score += 10 if (vix and 11 <= vix <= 22) else 0
    score += 10 if dte is not None and 1 <= dte <= 4 else (5 if dte == 0 else 0)
    return int(round(score))


def _structure(df, expiry, core_strikes: Dict[str, float], wing: float, lot: int, sig_type: str,
               naked_default: bool) -> Optional[Dict]:
    """Build legs + hedge legs + risk numbers for a short structure."""
    legs, hedges = [], []
    for opt, strike in core_strikes.items():
        leg = _leg(df, strike, opt, "SELL", "CORE", expiry)
        if not leg:
            return None
        legs.append(leg)
        hs = strike + wing if opt == "CE" else strike - wing
        h = _leg(df, _nearest_strike(df, hs), opt, "BUY", "HEDGE", expiry)
        if h:
            hedges.append(h)
    credit = sum(l["price"] for l in legs)
    hedge_cost = sum(h["price"] for h in hedges)
    sl_mult, tgt = settings.sell_sl_multiple, settings.sell_target_fraction
    sl_credit_total = credit * sl_mult                       # combined premium stop
    hedged = len(hedges) == len(legs)
    net = credit - (hedge_cost if hedged else 0)
    real_wing = None
    if hedged:
        real_wing = min(abs(h["strike"] - l["strike"]) for h, l in zip(hedges, legs))
    return {
        "type": sig_type, "legs": legs, "hedge_legs": hedges, "lot_size": lot,
        "defined_risk": hedged, "naked_if_unhedged": naked_default,
        "credit_points": round(credit, 2), "net_credit_hedged_points": round(net, 2) if hedged else None,
        "stop_loss_points": round(sl_credit_total, 2),                  # exit if combined premium reaches this
        "target_points": round(credit * (1 - tgt), 2),                  # exit when combined premium decays to this
        "max_profit_inr": round((net if hedged else credit) * lot, 2),
        "max_loss_inr": round(((real_wing - net) * lot), 2) if hedged else None,
        "sl_loss_inr": round((sl_credit_total - credit) * lot, 2),
    }


def generate_sell_signals(ctx: Dict) -> Dict:
    """ctx: symbol, spot, df, expiry, dte, vix, iv (iv_rank_percentile dict), metrics (chain_metrics),
    adx (optional), chain (optional, for lot_size), holdings_lots (optional)."""
    sym, spot, df, expiry = ctx["symbol"], ctx.get("spot") or 0, ctx.get("df"), ctx.get("expiry", "")
    m, iv, dte, vix = ctx.get("metrics") or {}, ctx.get("iv") or {}, ctx.get("dte"), ctx.get("vix") or 0
    lot = resolve_lot_size(sym, ctx.get("chain"))
    out = {"symbol": sym, "expiry": expiry, "dte": dte, "lot_size": lot, "signals": [], "wait_reasons": [],
           "context": {"iv_rank": iv.get("rank"), "iv_percentile": iv.get("percentile"), "iv_samples": iv.get("n"),
                       "pcr": m.get("pcr"), "pcr_bias": m.get("pcr_bias"), "max_pain": m.get("max_pain"),
                       "atm_iv": m.get("atm_iv"), "vix": vix, "call_wall": m.get("call_wall"),
                       "put_wall": m.get("put_wall"), "sigma_move": m.get("sigma_move")}}
    w = out["wait_reasons"]
    if df is None or df.empty or not spot or not m.get("ok"):
        w.append("Option chain / ATM quote முழுமையாக இல்லை — சிக்னல் இல்லை.")
        return out
    if not lot:
        w.append(f"{sym} lot size தெரியவில்லை — சிக்னல் இல்லை.")
        return out
    if not iv.get("reliable"):
        w.append(f"IV வரலாறு போதாது ({iv.get('n', 0)}/{iv.get('min_samples', settings.iv_rank_min_samples)} நாட்கள்) — "
                 "IV Rank நம்பகமில்லை; SELL சிக்னல் இல்லை. (நாள்தோறும் தானாக சேரும்.)")
        return out

    rank, step = iv["rank"], m["strike_step"]
    rb = _range_bound(m, spot, ctx.get("adx"))
    pcr_neutral = m.get("pcr_bias") == "NEUTRAL"
    if not rb["ok"]:
        w.append(f"சந்தை range-bound இல்லை ({rb['basis']}) — short straddle/strangle/condor தவிர்க்கவும்.")
    if rank < 30:
        w.append(f"IV Rank {rank} < 30 — பிரீமியம் மலிவு; விற்பனைக்கு ஏற்றதல்ல.")
    if vix and vix > 25:
        w.append(f"India VIX {vix} > 25 — நிகழ்வு/அதிக அபாயம்; naked sell தடை.")

    sigma = m["sigma_move"]
    ce_target = max(spot + sigma, m.get("call_wall") or 0)
    pe_target = min(spot - sigma, m.get("put_wall") or 1e12)
    ce_k = _nearest_strike(df, _round_up(ce_target, step))
    pe_k = _nearest_strike(df, _round_down(pe_target, step))
    atm = m["atm_strike"]
    wing = max(2 * step, _round_up(sigma * 0.5, step))
    conf = _confidence(rank, rb["ok"], pcr_neutral, vix, dte)
    base = {"symbol": sym, "spot": spot, "expiry": expiry, "dte": dte, "confidence": conf,
            "expiry_note": ("இன்று expiry — 3 PM-க்குள் மூடவும் (Close by 3 PM); defined-risk மட்டும்."
                            if dte == 0 else None)}

    def add(sig_type, core, naked_default, reasons):
        s = _structure(df, expiry, core, wing, lot, sig_type, naked_default)
        if not s:
            w.append(f"{sig_type}: தேவையான strike quotes இல்லை/0 — தவிர்க்கப்பட்டது.")
            return
        bad = [l for l in s["legs"] if l["spread_pct"] > 8]
        if bad:
            w.append(f"{sig_type}: bid-ask spread >8% ({bad[0]['strike']}{bad[0]['option_type']}) — liquidity குறைவு, தவிர்க்கப்பட்டது.")
            return
        s.update(base)
        s["id"] = f"{sym}-{expiry}-{sig_type}-" + "-".join(f"{l['strike']:.0f}{l['option_type']}" for l in s["legs"])
        s["reasons"] = reasons
        if not s["defined_risk"]:
            s["warnings"] = ["Wings quote இல்லை — naked (வரம்பற்ற அபாயம்)."]
        out["signals"].append(s)

    if rb["ok"] and rank > 70 and not (vix and vix > 25):
        why = [f"IV Rank {rank} > 70", f"Range-bound: {rb['basis']}", f"PCR {m['pcr']} ({m['pcr_bias']})",
               f"Strikes ≈1σ ({sigma:.0f} pts) / OI-wall-க்கு அப்பால்"]
        if dte != 0:   # naked-style straddle/strangle not offered on expiry day
            add("SHORT_STRANGLE", {"CE": ce_k, "PE": pe_k}, True, why)
            add("SHORT_STRADDLE", {"CE": atm, "PE": atm}, True, why)
    if rb["ok"] and rank >= 30:
        why = [f"IV Rank {rank} ≥ 30", f"Range-bound: {rb['basis']}", f"PCR {m['pcr']} ({m['pcr_bias']})",
               f"Short strikes ≈1σ; wings {wing:.0f} pts (defined risk)"]
        add("IRON_CONDOR", {"CE": ce_k, "PE": pe_k}, False, why)

    hl = ctx.get("holdings_lots") or 0
    if hl > 0 and rank > 60:
        s = _structure(df, expiry, {"CE": ce_k}, wing, lot, "COVERED_CALL", False)
        if s:
            s.update(base)
            s.update({"id": f"{sym}-{expiry}-COVERED_CALL-{ce_k:.0f}CE", "hedge_legs": [], "defined_risk": True,
                      "executable": False, "reasons": [f"IV Rank {rank} > 60", f"{hl} lot holding உள்ளது"],
                      "warnings": ["Advisory மட்டும் — stock/ETF holding தேவை; இந்த app அதை execute செய்யாது."]})
            out["signals"].append(s)

    for s in out["signals"]:
        s.setdefault("executable", True)
    out["signals"].sort(key=lambda s: (-s["confidence"], not s["defined_risk"]))
    return out
