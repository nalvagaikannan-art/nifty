"""
NIFTY-PRO — option-chain metrics for the SELL-signal engine
============================================================
Pure functions (no I/O) so they are easy to test:

  * iv_rank_percentile()  — where today's ATM IV sits vs its own history
  * chain_metrics()       — ATM strike/IV/straddle, PCR, max pain, OI walls

IV Rank = (IV - min) / (max - min) * 100        (over the stored history)
IV Percentile = % of stored days with IV below today's IV

Both need history. The history table (iv_history.py) fills one row per
trading day, so rank is flagged `reliable=False` until `min_samples` days
exist — the sell engine will NOT emit actionable signals on an unreliable
rank (it says so instead of guessing).
"""
from typing import Dict, List, Optional

import pandas as pd

from app.services.option_analyzer import OptionAnalyzer


def iv_rank_percentile(current: float, history: List[float], min_samples: int = 30) -> Dict:
    vals = [float(v) for v in history if v is not None and float(v) > 0]
    n = len(vals)
    out = {"iv": round(float(current or 0), 2), "n": n, "rank": None, "percentile": None,
           "low": None, "high": None, "reliable": False, "min_samples": min_samples}
    if not current or current <= 0 or n == 0:
        return out
    lo, hi = min(vals), max(vals)
    out["low"], out["high"] = round(lo, 2), round(hi, 2)
    if hi > lo:
        out["rank"] = round(max(0.0, min(100.0, (current - lo) / (hi - lo) * 100)), 1)
    out["percentile"] = round(sum(1 for v in vals if v < current) / n * 100, 1)
    out["reliable"] = n >= min_samples and out["rank"] is not None
    return out


def strike_step(df: pd.DataFrame) -> float:
    strikes = sorted(set(float(s) for s in df["strike"] if s and s > 0))
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b - a > 0]
    return min(diffs) if diffs else 50.0


def _price(row, side: str) -> float:
    mid = float(row.get(f"{side}_mid", 0) or 0)
    ltp = float(row.get(f"{side}_ltp", 0) or 0)
    return mid if mid > 0 else ltp


def pcr_bias(pcr: float) -> str:
    if pcr >= 1.2:
        return "BULLISH"      # heavy put writing
    if 0 < pcr <= 0.7:
        return "BEARISH"      # heavy call writing
    return "NEUTRAL"


def chain_metrics(df: pd.DataFrame, spot: float) -> Dict:
    """ATM data, PCR, max pain and OI walls from OptionAnalyzer's DataFrame."""
    if df is None or df.empty or not spot or spot <= 0:
        return {"ok": False, "reason": "chain_empty"}
    an = OptionAnalyzer()
    step = strike_step(df)
    atm_idx = (df["strike"] - spot).abs().idxmin()
    atm = df.loc[atm_idx]
    ivs = [v for v in (float(atm.get("ce_iv", 0) or 0), float(atm.get("pe_iv", 0) or 0)) if v > 0]
    atm_iv = round(sum(ivs) / len(ivs), 2) if ivs else 0.0
    ce_p, pe_p = _price(atm, "ce"), _price(atm, "pe")
    straddle = round(ce_p + pe_p, 2) if ce_p > 0 and pe_p > 0 else 0.0
    pcr = an.compute_pcr(df)
    call_wall = float(df.loc[df["ce_oi"].idxmax(), "strike"]) if df["ce_oi"].max() > 0 else 0.0
    put_wall = float(df.loc[df["pe_oi"].idxmax(), "strike"]) if df["pe_oi"].max() > 0 else 0.0
    return {
        "ok": atm_iv > 0 and straddle > 0,
        "reason": None if (atm_iv > 0 and straddle > 0) else "atm_quote_missing",
        "atm_strike": float(atm["strike"]), "strike_step": step,
        "atm_iv": atm_iv, "atm_ce": ce_p, "atm_pe": pe_p, "straddle": straddle,
        # ATM straddle ≈ 0.8 x one-sigma move  ->  1σ ≈ 1.25 x straddle
        "sigma_move": round(straddle * 1.25, 2),
        "pcr": pcr, "pcr_bias": pcr_bias(pcr),
        "max_pain": an.compute_max_pain(df),
        "call_wall": call_wall, "put_wall": put_wall,
    }
