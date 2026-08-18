"""
Strategy Router v5 — V2
=========================
Multi-factor scored strategy candidates + V2 engines:
  Confluence Engine, Market Regime, Trade Levels, Risk Engine

BUY CE / BUY PE / SELL CE / SELL PE / WAIT

Order placement இல்லை — Analysis + Decision Support மட்டும்.
"""
from fastapi import APIRouter, Depends, HTTPException
from app.services.market_analyzer import MarketAnalyzer
from app.services.ai_engine import AIEngine
from app.services.strategy_engine import generate_option_strategy, generate_price_levels
from app.services.strategy_history import record_signal, get_history
from app.services.confluence_engine import run_confluence_engine
from app.services.market_regime import classify_market_regime
from app.services.trade_levels import calculate_trade_levels
from app.services.risk_engine import assess_risk
from app.services.paper_trading import get_open_trades, get_daily_pnl
from app.exceptions import MarketDataError, AIProviderError
from app.api.deps import get_analyzer, get_ai_engine
from app.utils.helpers import safe_float, expiry_filter, days_to_expiry as _days_to_expiry
from app.services.options_greeks import black_scholes_greeks, mid_price, spread_pct
from app.config import settings
from datetime import datetime
from zoneinfo import ZoneInfo
import logging, math

_IST = ZoneInfo("Asia/Kolkata")

router = APIRouter()
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 FILTERS
# ═══════════════════════════════════════════════════════════════════════════

# ── 2A. IV Filter ─────────────────────────────────────────────────────────
def _iv_filter(atm_iv: float, strategy: str) -> dict:
    """
    IV மிகவும் முக்கியம்.
    BUY CE/PE: IV high → premium expensive → score penalize
    SELL CE/PE: IV high → good premium → score boost (but strict filter)
    """
    is_buy  = strategy.startswith("BUY")
    is_sell = strategy.startswith("SELL")

    if atm_iv <= 0:
        return {"penalty": 0, "note": "", "warning": ""}

    if is_buy:
        if atm_iv > 40:
            return {"penalty": -25, "note": f"IV {atm_iv:.1f}% மிகவும் high — premium buying risky",
                    "warning": "⚠️ HIGH IV — Premium costly"}
        elif atm_iv > 30:
            return {"penalty": -12, "note": f"IV {atm_iv:.1f}% elevated — premium slightly costly",
                    "warning": "⚠️ ELEVATED IV"}
        elif atm_iv < 10:
            return {"penalty": 5, "note": f"IV {atm_iv:.1f}% low — cheap premium, good for buying",
                    "warning": ""}
        return {"penalty": 0, "note": f"IV {atm_iv:.1f}% — acceptable", "warning": ""}

    if is_sell:
        if atm_iv >= 25:
            return {"penalty": 10, "note": f"IV {atm_iv:.1f}% — rich premium, good for selling",
                    "warning": ""}
        elif atm_iv >= 18:
            return {"penalty": 5,  "note": f"IV {atm_iv:.1f}% — decent premium", "warning": ""}
        else:
            return {"penalty": -15, "note": f"IV {atm_iv:.1f}% low — premium thin, selling risky",
                    "warning": "⚠️ LOW IV — Selling premium thin"}

    return {"penalty": 0, "note": "", "warning": ""}


# ── 2B. Liquidity Filter ──────────────────────────────────────────────────
def _liquidity_filter(oi: float, volume: float, ltp: float) -> dict:
    """
    Illiquid options reject செய்கிறோம்.
    """
    issues = []
    if oi < 500:      issues.append(f"OI too low ({oi:.0f})")
    if volume < 200:  issues.append(f"Volume low ({volume:.0f})")
    if ltp <= 0:      issues.append("No LTP")
    if ltp > 0 and oi > 0 and volume == 0:
        issues.append("Zero volume today")

    if issues:
        return {"liquid": False, "reason": " | ".join(issues),
                "warning": "⚠️ LIQUIDITY TOO LOW"}
    return {"liquid": True, "reason": "", "warning": ""}


# ── 2C. Expiry Day Filter ─────────────────────────────────────────────────
def _expiry_filter(expiry_str: str) -> dict:
    """Kept as a thin wrapper for backward compatibility — logic now lives
    in app.utils.helpers.expiry_filter (shared with /api/analysis, which
    previously had no expiry-day warning at all)."""
    return expiry_filter(expiry_str)


# ── 2D. Time-of-Day Filter ────────────────────────────────────────────────
def _time_filter() -> dict:
    """
    Opening (9:15-9:45): High volatility, false signals possible
    Mid-session (9:45-14:00): Most reliable
    Closing (14:00-15:30): Fast momentum, gamma considerations

    BUG FIX (2026-08-16): datetime.now() was server-local time — on Render
    the server runs UTC, so IST 9:15 = UTC 3:45. All session checks were
    wrong by 5h30m. Fixed: datetime.now(_IST) forces Asia/Kolkata time.
    """
    now = datetime.now(_IST)
    hour, minute = now.hour, now.minute
    total_min = hour * 60 + minute

    # IST market hours: 9:15 AM to 3:30 PM
    open_min  = 9 * 60 + 15   # 555
    close_min = 15 * 60 + 30  # 930

    if total_min < open_min or total_min > close_min:
        return {"session": "CLOSED", "penalty": 20,
                "warning": "Market closed", "note": ""}

    mins_since_open = total_min - open_min

    if mins_since_open < 30:  # 9:15 - 9:45
        return {"session": "OPENING", "penalty": -10,
                "warning": "⚠️ OPENING VOLATILITY — Wait for range to establish",
                "note": "First 30 minutes: false signals more likely"}
    elif total_min > 14 * 60:  # 2:00 PM onwards
        return {"session": "CLOSING", "penalty": -5,
                "warning": "⚠️ CLOSING SESSION — Fast moves, expiry gamma",
                "note": "Last 90 minutes: faster momentum + gamma effects"}
    else:
        return {"session": "MID", "penalty": 0,
                "warning": "", "note": "Mid-session: most reliable signals"}


# ── 2E. Anti-Whipsaw (Score Hysteresis) ──────────────────────────────────
_prev_best: dict = {}  # {symbol: {"strategy": str, "score": int, "ts": float}}

def _anti_whipsaw(symbol: str, new_best: str, new_score: int) -> dict:
    """
    Signal 15 seconds-க்கு ஒரு முறை மாறக்கூடாது.
    Score margin < 15 ஆக இருந்தால் previous strategy தக்கவைக்கும்.
    Strong breakout (score > 75) மட்டும் immediate reversal அனுமதிக்கும்.
    """
    import time
    prev = _prev_best.get(symbol, {})
    prev_strategy = prev.get("strategy", "")
    prev_score    = prev.get("score", 0)
    prev_ts       = prev.get("ts", 0)
    now           = time.time()

    # First signal
    if not prev_strategy:
        _prev_best[symbol] = {"strategy": new_best, "score": new_score, "ts": now}
        return {"strategy": new_best, "changed": True, "whipsaw_blocked": False}

    # Strong signal → allow immediate change
    if new_score >= 75:
        _prev_best[symbol] = {"strategy": new_best, "score": new_score, "ts": now}
        return {"strategy": new_best, "changed": new_best != prev_strategy,
                "whipsaw_blocked": False}

    # Score margin check — if margin < 15, keep previous
    if prev_strategy != new_best and new_score - prev_score < 15:
        return {"strategy": prev_strategy, "changed": False,
                "whipsaw_blocked": True,
                "note": f"Score margin ({new_score - prev_score}) < 15 — keeping {prev_strategy}"}

    _prev_best[symbol] = {"strategy": new_best, "score": new_score, "ts": now}
    return {"strategy": new_best, "changed": new_best != prev_strategy,
            "whipsaw_blocked": False}


# ── 2F. Market State Classifier ──────────────────────────────────────────
def _classify_market_state(market_data: dict) -> str:
    """
    Specification section 6 — 11 market states.
    """
    dec    = market_data.get("decision", {})
    margin = dec.get("margin", 0)
    adx    = market_data.get("technicals", {}).get("adx", 0)
    vix    = market_data.get("vix", 15)
    rsi    = market_data.get("rsi", 50)
    spot   = market_data.get("spot", {}).get("price", 0)
    ema20  = market_data.get("technicals", {}).get("ema20", 0)
    prev_spot = market_data.get("spot", {}).get("prev_close", 0)

    gap_pct = ((spot - prev_spot) / prev_spot * 100) if prev_spot > 0 else 0

    if vix > 25:           return "HIGH VOLATILITY"
    if gap_pct > 1.5:      return "BREAKOUT"
    if gap_pct < -1.5:     return "BREAKDOWN"
    if margin >= 25 and adx >= 25:  return "STRONG BULLISH"
    if margin >= 15:                return "BULLISH"
    if margin >= 8:                 return "MILD BULLISH"
    if margin <= -25 and adx >= 25: return "STRONG BEARISH"
    if margin <= -15:               return "BEARISH"
    if margin <= -8:                return "MILD BEARISH"
    if adx < 18:                    return "RANGE BOUND"
    return "NEUTRAL"


# ── Strategy Candidate Scorer ─────────────────────────────────────────────

def _score_candidates(market_data: dict) -> dict:
    """
    5 strategies score செய்கிறோம் (0-100 each).
    Returns: {strategy_name: score, ...} + winning strategy info.
    """
    dec        = market_data.get("decision", {})
    bull       = dec.get("bull_score", 0)
    bear       = dec.get("bear_score", 0)
    margin     = dec.get("margin", bull - bear)
    adx        = market_data.get("technicals", {}).get("adx", 0)
    vix        = market_data.get("vix", 15)
    pcr        = market_data.get("pcr", 0)
    atr        = market_data.get("technicals", {}).get("atr", 0)
    spot       = market_data.get("spot", {}).get("price", 0)
    preferred  = dec.get("preferred_side", "NONE")
    confidence = dec.get("confidence", 30)
    oi_sum     = market_data.get("oi_summary", {})
    ce_max_strike = oi_sum.get("ce_max_oi_strike", 0)
    pe_max_strike = oi_sum.get("pe_max_oi_strike", 0)
    atm_iv     = oi_sum.get("atm_iv", 0)
    atr_pct    = (atr / spot * 100) if spot > 0 else 0
    market_open = market_data.get("spot", {}).get("market_open", True)

    # ── BUY CE Score ──────────────────────────────────────────────────────
    buy_ce = 0
    if preferred == "CALL":
        buy_ce += min(margin * 1.5, 35)          # direction score
    buy_ce += min(bull * 0.4, 20)                # bull score contribution
    if adx >= 20:       buy_ce += 15             # trend confirmation
    if adx >= 25:       buy_ce += 5              # strong trend bonus
    if vix < 16:        buy_ce += 10             # low fear
    elif vix > 22:      buy_ce -= 15             # high IV = expensive premium
    if pcr > 1.1:       buy_ce += 8              # put writing support
    if pcr < 0.8:       buy_ce -= 10             # bearish PCR
    if atm_iv > 25:     buy_ce -= 12             # overpriced premium
    if atm_iv > 35:     buy_ce -= 10             # very overpriced
    # Resistance check — CE max OI near spot = resistance, bad for buying
    if ce_max_strike > 0 and spot > 0:
        ce_dist_pct = (ce_max_strike - spot) / spot * 100
        if 0 < ce_dist_pct < 1.5:  buy_ce -= 12  # wall just above
        elif ce_dist_pct > 3:      buy_ce += 5   # wall far away
    buy_ce = max(0, min(100, buy_ce))

    # ── BUY PE Score ──────────────────────────────────────────────────────
    buy_pe = 0
    if preferred == "PUT":
        buy_pe += min(abs(margin) * 1.5, 35)
    buy_pe += min(bear * 0.4, 20)
    if adx >= 20:       buy_pe += 15
    if adx >= 25:       buy_pe += 5
    if vix < 16:        buy_pe += 10
    elif vix > 22:      buy_pe -= 15
    if pcr < 0.8:       buy_pe += 8
    if pcr > 1.2:       buy_pe -= 10
    if atm_iv > 25:     buy_pe -= 12
    if atm_iv > 35:     buy_pe -= 10
    # Support check — PE max OI below spot = support, bad for put buying
    if pe_max_strike > 0 and spot > 0:
        pe_dist_pct = (spot - pe_max_strike) / spot * 100
        if 0 < pe_dist_pct < 1.5:  buy_pe -= 12  # floor just below
        elif pe_dist_pct > 3:      buy_pe += 5
    buy_pe = max(0, min(100, buy_pe))

    # ── SELL CE Score ─────────────────────────────────────────────────────
    # Strict: needs strong resistance + IV attractive + no breakout risk
    sell_ce = 0
    if ce_max_strike > 0 and spot > 0:
        dist = (ce_max_strike - spot) / spot * 100
        if 1.0 < dist < 2.5:   sell_ce += 30    # sweet spot: OTM but close
        elif 0.5 < dist <= 1.0: sell_ce += 15   # too close = breakout risk
    if atm_iv >= 18:    sell_ce += 15            # decent premium
    if atm_iv >= 25:    sell_ce += 10            # rich premium
    if vix < 18:        sell_ce += 10            # stable VIX
    if margin < -5:     sell_ce += 10            # bearish bias helps SELL CE
    if margin > 15:     sell_ce -= 25            # strong bull = breakout risk
    if adx >= 25 and preferred == "CALL":
        sell_ce -= 20                             # trending bull = don't sell CE
    if bull > 30:       sell_ce -= 10
    sell_ce = max(0, min(100, sell_ce))

    # ── SELL PE Score ─────────────────────────────────────────────────────
    sell_pe = 0
    if pe_max_strike > 0 and spot > 0:
        dist = (spot - pe_max_strike) / spot * 100
        if 1.0 < dist < 2.5:   sell_pe += 30
        elif 0.5 < dist <= 1.0: sell_pe += 15
    if atm_iv >= 18:    sell_pe += 15
    if atm_iv >= 25:    sell_pe += 10
    if vix < 18:        sell_pe += 10
    if margin > 5:      sell_pe += 10            # bullish bias helps SELL PE
    if margin < -15:    sell_pe -= 25            # strong bear = breakdown risk
    if adx >= 25 and preferred == "PUT":
        sell_pe -= 20
    if bear > 30:       sell_pe -= 10
    sell_pe = max(0, min(100, sell_pe))

    # ── WAIT Score ────────────────────────────────────────────────────────
    wait = 0
    if abs(margin) < 8:         wait += 30       # no clear direction
    if adx < 18:                wait += 20       # no trend
    if not market_open:         wait += 50       # market closed
    if confidence < 40:         wait += 15
    wait = max(0, min(100, wait))

    candidates = {
        "BUY CE":  round(buy_ce),
        "BUY PE":  round(buy_pe),
        "SELL CE": round(sell_ce),
        "SELL PE": round(sell_pe),
        "WAIT":    round(wait),
    }

    # Best candidate
    best = max(candidates, key=candidates.get)

    # Minimum threshold — if best < 45, default WAIT
    if candidates[best] < 45 and best != "WAIT":
        best = "WAIT"

    return {"candidates": candidates, "best": best, "best_score": candidates[best]}


# ── Strike Picker (liquidity + delta-band + spread hard filters) ──────────
# Review point #26/#4 (priority): "ATM+1 என்றால் எப்போதும் safer இல்லை ...
# strike selection-ல் Delta range கூட hard rule ஆக வேண்டும்." Previously
# "Best Strike" was blindly ATM+1/ATM-1 regardless of how that specific
# strike's liquidity/spread/delta actually looked. Now we score every
# nearby strike and hard-filter on liquidity + spread + a preferred delta
# band, so "Best Strike" is the best ACTUAL candidate, not just the closest
# to ATM by index.
HARD_SPREAD_MAX_PCT   = 8.0    # wider than this bid/ask spread → excluded outright, not just warned
PREFERRED_DELTA_LOW    = 0.35  # review's example liquid zone: 0.45-0.60; widened slightly so a pick usually exists
PREFERRED_DELTA_HIGH   = 0.65


def _pick_strikes(chain_data: dict, is_call: bool, spot: float, atr: float = 0.0) -> list:
    """`atr` (optional, underlying ATR in points): when provided, SL/targets
    are computed from real delta × ATR (mirrors trade_levels.py's model)
    instead of a flat percentage of premium — review #38: a fixed 35%/40%
    SL/target made no sense across strikes with very different deltas."""
    rows = chain_data.get("data", [])
    if not rows:
        return []

    key_oi = "CE" if is_call else "PE"
    strikes = sorted(set(r["strikePrice"] for r in rows))
    if not strikes:
        return []

    atm = min(strikes, key=lambda s: abs(s - spot))
    atm_idx = strikes.index(atm)

    contract_expiry = chain_data.get("expiry", "")  # live from the option chain fetch — never hardcoded
    dte = _days_to_expiry(contract_expiry)

    # Build lookup with liquidity check
    lkp = {}
    for r in rows:
        s = r["strikePrice"]
        side = r.get(key_oi, {}) or {}
        ltp = safe_float(side.get("lastPrice", 0))
        oi  = safe_float(side.get("openInterest", 0))
        vol = safe_float(side.get("totalTradedVolume", 0))
        iv  = safe_float(side.get("impliedVolatility", 0))
        bid = safe_float(side.get("bidprice", 0))
        ask = safe_float(side.get("askPrice", 0))
        # Liquidity check
        liquid = ltp > 0 and (oi > 100 or vol > 100)
        lkp[s] = {"ltp": ltp, "oi": oi, "volume": vol, "iv": iv,
                  "bid": bid, "ask": ask, "liquid": liquid}

    def make_strike(strike, label, emoji, note):
        info = lkp.get(strike, {})
        if not info.get("liquid"):
            return None
        # Review #13: entry/SL/target off the executable mid-price when a
        # usable bid/ask quote exists — LTP alone can be a stale last trade
        # on a thin strike and overstate what you'd actually get filled at.
        entry  = mid_price(info["bid"], info["ask"], info["ltp"])
        spread = spread_pct(info["bid"], info["ask"])
        # Hard filter (not just a warning): a spread this wide means slippage
        # alone could eat the whole expected edge — don't recommend it.
        if spread is not None and spread > HARD_SPREAD_MAX_PCT:
            return None
        # Review #15: Greeks — None (not fabricated) when spot/IV/expiry
        # don't support a real Black-Scholes calc.
        greeks = black_scholes_greeks(spot, strike, dte, info["iv"], "CE" if is_call else "PE")
        strike_delta = greeks["delta"] if greeks else None
        theta_per_day = greeks["theta_per_day"] if greeks else None

        # SL/targets: prefer real delta × ATR (review #38) over a flat %,
        # since the same premium % target is not equally realistic across
        # strikes with very different deltas. Falls back to the old flat-%
        # model only when ATR or Greeks aren't available.
        if atr > 0 and strike_delta is not None:
            opt_risk = round(atr * abs(strike_delta), 1)
            sl = round(entry - opt_risk, 1)
            t1 = round(entry + opt_risk * 1.0, 1)
            t2 = round(entry + opt_risk * 2.0, 1)
            t3 = round(entry + opt_risk * 2.5, 1)
            if theta_per_day is not None and theta_per_day < 0:
                decay = round(abs(theta_per_day), 1)  # 1-day decay netted out of targets
                t1 = round(t1 - decay, 1)
                t2 = round(t2 - decay, 1)
                t3 = round(t3 - decay, 1)
            sl = max(sl, round(entry * 0.40, 1))  # never let the modelled SL go below the 40% floor
            levels_basis = "delta×ATR, net of 1-day theta"
        else:
            sl = round(entry * 0.65, 1)   # 35% SL
            t1 = round(entry * 1.40, 1)   # T1: +40%
            t2 = round(entry * 1.70, 1)   # T2: +70%
            t3 = round(entry * 2.00, 1)   # T3: +100%
            levels_basis = "flat % approximation (ATR/delta unavailable)"
        risk   = round(entry - sl, 1)
        reward = round(t1 - entry, 1)
        rr     = round(reward / risk, 1) if risk > 0 else 0

        result = {
            "rank":    emoji,
            "label":   label,
            "strike":  strike,
            "type":    "CE" if is_call else "PE",
            "expiry":  contract_expiry,   # exact contract this pick is for — same chain fetch as strike/ltp/oi above
            "ltp":     round(info["ltp"], 2),
            "entry_price": entry,          # mid-price estimate — use this for P&L math, not ltp
            "bid":     round(info["bid"], 2),
            "ask":     round(info["ask"], 2),
            "spread_pct": spread,
            "oi":      int(info["oi"]),
            "volume":  int(info["volume"]),
            "iv":      round(info["iv"], 1),
            "sl":      sl,
            "t1":      t1,
            "t2":      t2,
            "t3":      t3,
            "rr":      rr,
            "levels_basis": levels_basis,
            "note":    note,
        }
        if greeks:
            result["delta"] = greeks["delta"]
            result["theta_per_day"] = greeks["theta_per_day"]
            result["vega"] = greeks["vega"]
            # Review #15: theta as a % of entry price — the same absolute
            # ₹/day means very different things for a ₹300 vs ₹30 option.
            result["theta_pct_of_entry"] = (
                round(abs(greeks["theta_per_day"]) / entry * 100, 1) if entry > 0 else None
            )
        if spread is not None and spread > 4.0:
            result["note"] = note + f" ⚠️ Wide bid/ask spread ({spread:.1f}%) — slippage risk on entry/exit"
        return result

    # ── "Best Strike": score every nearby strike, hard-filter on liquidity
    # + spread (inside make_strike) + delta band, pick the one closest to
    # the review's suggested 0.45-0.60 liquid zone (widened to 0.35-0.65
    # here so a candidate almost always exists) instead of blindly ATM+1.
    candidate_offsets = [1, 2, 0, 3, -1] if is_call else [-1, -2, 0, -3, 1]
    best_pick = None
    best_delta_gap = None
    fallback_pick = None
    for off in candidate_offsets:
        idx = max(0, min(atm_idx + off, len(strikes) - 1))
        cand = make_strike(strikes[idx], "Best Strike", "🥇",
                            f"Auto-selected — liquid, tight spread, delta in {PREFERRED_DELTA_LOW}-{PREFERRED_DELTA_HIGH} band")
        if cand is None:
            continue
        if fallback_pick is None:
            fallback_pick = cand  # first liquid+spread-ok candidate, in case none hit the delta band
        d = cand.get("delta")
        if d is not None and PREFERRED_DELTA_LOW <= abs(d) <= PREFERRED_DELTA_HIGH:
            gap = abs(abs(d) - 0.50)
            if best_delta_gap is None or gap < best_delta_gap:
                best_delta_gap = gap
                best_pick = cand
    if best_pick is None and fallback_pick is not None:
        fallback_pick["note"] = "No strike found in the preferred delta band nearby — showing best available liquid strike instead"
        best_pick = fallback_pick

    results = [best_pick]
    if is_call:
        results.append(make_strike(strikes[min(atm_idx+2, len(strikes)-1)],
            "Aggressive (OTM)", "🥈", "OTM — Cheaper premium, bigger move needed"))
        results.append(make_strike(strikes[max(atm_idx-1, 0)],
            "Conservative (ITM)", "🥉", "ITM — Safer, already has intrinsic value"))
    else:
        results.append(make_strike(strikes[max(atm_idx-2, 0)],
            "Aggressive (OTM)", "🥈", "OTM — Cheaper premium, bigger move needed"))
        results.append(make_strike(strikes[min(atm_idx+1, len(strikes)-1)],
            "Conservative (ITM)", "🥉", "ITM — Safer, already has intrinsic value"))

    return [r for r in results if r is not None]


# ── Expected Move (VIX-based) ─────────────────────────────────────────────

def _expected_move(spot: float, vix: float, days_to_expiry: int = 7) -> dict:
    """India VIX அடிப்படையில் expected move calculate செய்கிறோம்."""
    if spot <= 0 or vix <= 0:
        return {}
    annual_vol = vix / 100
    daily_move = annual_vol / math.sqrt(252)
    period_move = daily_move * math.sqrt(days_to_expiry)
    move_pts    = round(spot * period_move, 0)
    return {
        "move_points": move_pts,
        "upper":       round(spot + move_pts, 0),
        "lower":       round(spot - move_pts, 0),
        "basis":       f"VIX {vix:.1f} → ±{move_pts:.0f} points expected in ~{days_to_expiry} days",
    }


# ── Main Endpoint ─────────────────────────────────────────────────────────

@router.get("/recommend/{symbol}")
async def strike_recommendation(
    symbol: str,
    expiry: str = None,
    analyzer: MarketAnalyzer = Depends(get_analyzer),
    ai: AIEngine = Depends(get_ai_engine),
):
    """
    Phase 1 Strategy Engine:
    - 5 candidate scoring (BUY CE/PE, SELL CE/PE, WAIT)
    - Liquidity filter
    - SL + T1/T2/T3 for each strike
    - R:R calculation
    - Expected move (VIX-based)
    - Price levels (ATR-based Entry/SL/Target)
    - Strategy detail (legs from strategy_engine)

    `expiry`: optional, e.g. "28-Aug-2025" (must be one of the values in
    /api/options/chain/{symbol}'s all_expiries). Omit for the nearest
    expiry (previous/default behaviour).
    """
    try:
        market_data = await analyzer.get_full_market_overview(symbol, expiry=expiry)
    except MarketDataError as e:
        raise HTTPException(502, detail=f"Market data unavailable: {e}")

    dec        = market_data.get("decision", {})
    spot       = market_data.get("spot", {}).get("price", 0)
    max_pain   = market_data.get("max_pain", 0)
    chain      = market_data.get("option_chain", {})
    expiry     = chain.get("expiry", "")
    vix        = market_data.get("vix", 0)
    atm_iv     = market_data.get("oi_summary", {}).get("atm_iv", 0)

    # ── Phase 2A: Time filter ─────────────────────────────────────────────
    time_info  = _time_filter()

    # ── Phase 2B: Expiry filter ───────────────────────────────────────────
    expiry_info = _expiry_filter(expiry)
    days_to_exp = expiry_info.get("days_left", 7)

    # ── Phase 2C: Market state ────────────────────────────────────────────
    market_state = _classify_market_state(market_data)

    # ── 5-candidate scoring (with time penalty) ───────────────────────────
    scored     = _score_candidates(market_data)
    candidates = scored["candidates"]
    raw_best   = scored["best"]
    raw_score  = scored["best_score"]

    # Apply time penalty to all candidates
    if time_info["penalty"] != 0:
        for k in candidates:
            candidates[k] = max(0, candidates[k] + time_info["penalty"])
        raw_score = candidates.get(raw_best, raw_score)

    # ── Phase 2D: Anti-whipsaw ────────────────────────────────────────────
    whipsaw_result = _anti_whipsaw(symbol, raw_best, raw_score)
    best       = whipsaw_result["strategy"]
    best_score = candidates.get(best, raw_score)

    # ── Phase 2E: IV filter on best strategy ──────────────────────────────
    iv_info    = _iv_filter(atm_iv, best)
    if iv_info["penalty"] != 0:
        best_score = max(0, best_score + iv_info["penalty"])
        # If IV penalty drops best below threshold, downgrade to WAIT
        if best_score < 45 and best != "WAIT":
            best = "WAIT"
            best_score = candidates["WAIT"]

    # ── Market bias ───────────────────────────────────────────────────────
    market_bias = dec.get("market_bias", "Sideways")
    preferred   = dec.get("preferred_side", "NONE")

    # ── V2: Confluence Engine ─────────────────────────────────────────────
    confluence = {}
    try:
        confluence = run_confluence_engine(market_data)
    except Exception as _ce:
        logger.warning(f"Confluence engine error: {_ce}")
        confluence = {"confluence_score": 0, "direction": "NEUTRAL", "quality": "LOW",
                      "agreement_count": 0, "total_factors": 0, "factors": []}

    # ── V2: Market Regime ─────────────────────────────────────────────────
    regime = {}
    try:
        regime = classify_market_regime(market_data, confluence)
    except Exception as _re:
        logger.warning(f"Market regime error: {_re}")
        regime = {"regime": "UNKNOWN", "confidence": "LOW", "no_trade": False,
                  "preferred_strategy": "WAIT", "reasons": []}

    # ── V2: Signal strength (rename from confidence) ──────────────────────
    signal_strength = dec.get("signal_strength", dec.get("confidence", 0))

    # ── V2: Action label based on regime + best ───────────────────────────
    def _action_label(best_strat: str, reg: dict) -> str:
        no_trade = reg.get("no_trade", False)
        if no_trade:
            return "NO TRADE — " + (reg.get("no_trade_reason") or reg.get("regime", ""))
        if best_strat == "WAIT":
            return "WAIT FOR CONFIRMATION"
        if best_strat == "BUY CE":
            return "WAIT FOR BREAKOUT ABOVE TRIGGER"
        if best_strat == "BUY PE":
            return "WAIT FOR BREAKDOWN BELOW TRIGGER"
        if best_strat in ("SELL CE", "SELL PE"):
            return "WAIT FOR ENTRY NEAR STRIKE"
        return "WAIT"

    action_label = _action_label(best, regime)

    # ── V2: Strike picks (moved up so Trade Levels can reuse the SAME
    # recommended strike below, instead of a second, independent ATM-only
    # LTP lookup drifting from what's actually recommended) ───────────────
    _is_call_for_picks = "CE" in best if best in ("BUY CE", "BUY PE") else True
    _atr_for_picks = safe_float((market_data.get("technicals") or {}).get("atr", 0))
    raw_strikes = _pick_strikes(chain, _is_call_for_picks, spot, atr=_atr_for_picks) if best in ("BUY CE", "BUY PE") else []

    # ── V2: Trade levels (only for directional setups) ────────────────────
    # Bug fix: this previously always priced levels off the ATM strike's
    # LTP regardless of which strike was actually recommended ("simplified:
    # use ATM for levels") — now it reuses _pick_strikes' own "Best Strike"
    # pick (mid-price entry + real delta/theta), so the SL/T1/T2/T3 shown
    # here match the strike a user would actually buy, and (review #38)
    # are computed from that strike's real delta × ATR instead of a fixed
    # 0.45 approximation.
    v2_trade_levels = None
    try:
        if best in ("BUY CE", "BUY PE") and spot > 0:
            direction  = "bullish" if best == "BUY CE" else "bearish"
            _best_strike_pick = raw_strikes[0] if raw_strikes else None
            _opt_ltp   = _best_strike_pick["entry_price"] if _best_strike_pick else 0.0
            _delta     = _best_strike_pick.get("delta") if _best_strike_pick else None
            _theta     = _best_strike_pick.get("theta_per_day") if _best_strike_pick else None
            v2_trade_levels = calculate_trade_levels(
                market_data, direction, spot, _opt_ltp,
                delta=_delta, theta_per_day=_theta,
            )
    except Exception as _tl:
        logger.warning(f"Trade levels error: {_tl}")

    # ── V2: Risk assessment ───────────────────────────────────────────────
    v2_risk = None
    try:
        if v2_trade_levels and best in ("BUY CE", "BUY PE"):
            _entry = v2_trade_levels.get("option_entry") or 0
            _sl    = v2_trade_levels.get("option_sl") or 0
            _rr    = v2_trade_levels.get("rr_ratio", 1.5)
            _daily = await get_daily_pnl()
            _open_trades = await get_open_trades(symbol)
            v2_risk = assess_risk(
                capital          = settings.default_capital,  # BUG FIX: from config (DEFAULT_CAPITAL env var)
                entry_price      = _entry,
                stop_loss_price  = _sl,
                rr_ratio         = _rr,
                market_regime    = regime.get("regime", "UNKNOWN"),
                vix              = vix,
                days_to_expiry   = days_to_exp,
                confluence_quality = confluence.get("quality", "LOW"),
                signal_strength  = signal_strength,
                open_positions   = len(_open_trades),
                daily_pnl        = _daily,
            )
    except Exception as _rk:
        logger.warning(f"Risk engine error: {_rk}")

    # ── Expected move ─────────────────────────────────────────────────────
    exp_move = _expected_move(spot, vix, max(days_to_exp, 1))

    # ── WAIT state ────────────────────────────────────────────────────────
    if best == "WAIT":
        wait_reasons = []
        if dec.get("confidence", 0) < 40:
            wait_reasons.append("Confidence குறைவு")
        if dec.get("margin", 0) < 8 and dec.get("margin", 0) > -8:
            wait_reasons.append("Bull/Bear score close — no clear direction")
        tech = market_data.get("technicals", {})
        if tech.get("adx", 0) < 18:
            wait_reasons.append(f"ADX {tech.get('adx',0):.1f} < 18 — range bound")
        if not market_data.get("spot", {}).get("market_open", True):
            wait_reasons.append("Market closed")
        if time_info.get("warning"):
            wait_reasons.append(time_info["warning"])
        if iv_info.get("warning"):
            wait_reasons.append(iv_info["warning"])
        if whipsaw_result.get("whipsaw_blocked"):
            wait_reasons.append(whipsaw_result.get("note", "Anti-whipsaw filter"))

        warnings = []
        for info in [time_info, expiry_info, iv_info]:
            if info.get("warning"):
                warnings.append(info["warning"])

        # Record signal history
        sig = record_signal(
            symbol=symbol, strategy="WAIT", score=candidates["WAIT"],
            market_state=market_state, confidence=dec.get("confidence", 0),
            spot=spot, pcr=market_data.get("pcr", 0), vix=vix,
            reasons=wait_reasons,
        )

        return {
            "symbol":        symbol,
            "best_strategy": "WAIT",
            "best_score":    candidates["WAIT"],
            "candidates":    candidates,
            "market_bias":   market_bias,
            "market_state":  market_state,
            "spot":          spot,
            "expiry":        expiry,
            "all_expiries":  chain.get("all_expiries", []),
            "bull_score":    dec.get("bull_score", 0),
            "bear_score":    dec.get("bear_score", 0),
            "confidence":    dec.get("confidence", 0),
            "signal_strength": signal_strength,
            "max_pain":      max_pain,
            "pcr":           market_data.get("pcr", 0),
            "vix":           vix,
            "expected_move": exp_move,
            "strategy":      dec.get("strategy", ""),
            "strategy_reason": dec.get("strategy_reason", ""),
            "wait_reasons":  wait_reasons,
            "warnings":      warnings,
            "time_session":  time_info.get("session", ""),
            "expiry_info":   expiry_info,
            "iv_info":       iv_info,
            "signal_history": get_history(symbol),
            "signal_reversal": sig.get("reversal", False),
            "reversal_type":   sig.get("reversal_type", ""),
            "strikes":       [],
            "price_levels":  None,
            "strategy_detail": None,
            "ai_reason":     "No high-quality setup — WAIT.",
            "disclaimer":    "Not investment advice. Trade at your own risk.",
            # ── V2 fields ────────────────────────────────────────────────
            "confluence":        confluence,
            "market_regime":     regime,
            "action":            action_label,
            "v2_trade_levels":   None,
            "v2_risk":           None,
            "win_probability":   None,
            "win_probability_note": "Win Probability: Not enough historical data",
        }

    # ── Phase 2F: Strike picks + Liquidity filter ─────────────────────────
    is_call = "CE" in best
    # Reuse the picks already computed above (for BUY CE/PE, Trade Levels
    # needed them) instead of recomputing — SELL CE/PE still computes fresh.
    if not raw_strikes:
        raw_strikes = _pick_strikes(chain, is_call, spot, atr=_atr_for_picks)
    strikes = []
    liquidity_warnings = []
    for s in raw_strikes:
        liq = _liquidity_filter(s.get("oi", 0), s.get("volume", 0), s.get("ltp", 0))
        s["liquidity_ok"]      = liq["liquid"]
        s["liquidity_warning"] = liq["warning"]
        if not liq["liquid"]:
            liquidity_warnings.append(f"{s['strike']} {s['type']} ({s.get('expiry','')}): {liq['reason']}")
        strikes.append(s)

    # ── Price levels (ATR-based) ──────────────────────────────────────────
    price_levels = generate_price_levels(market_data)

    # ── Strategy detail (legs) ────────────────────────────────────────────
    import pandas as pd
    from app.services.option_analyzer import OptionAnalyzer
    opt_df = pd.DataFrame()
    try:
        if chain.get("data"):
            opt_df = OptionAnalyzer().process_option_chain(chain)
    except Exception:
        pass
    strategy_detail = generate_option_strategy(market_data, opt_df)

    # ── AI reasoning ──────────────────────────────────────────────────────
    ai_reason = ""
    try:
        ai_result = await ai.analyze_market(market_data)
        ai_reason = ai_result.get("reason", "")
    except AIProviderError:
        ai_reason = "AI unavailable — rule engine decision shown."

    # ── Collect all warnings ──────────────────────────────────────────────
    warnings = []
    for info in [time_info, expiry_info, iv_info]:
        if info.get("warning"):
            warnings.append(info["warning"])
    if liquidity_warnings:
        warnings.append("⚠️ Some strikes have low liquidity")
    if whipsaw_result.get("whipsaw_blocked"):
        warnings.append(f"Anti-whipsaw: {whipsaw_result.get('note','')}")

    # Record signal history
    sig = record_signal(
        symbol=symbol, strategy=best, score=best_score,
        market_state=market_state, confidence=dec.get("confidence", 0),
        spot=spot, pcr=market_data.get("pcr", 0), vix=vix,
    )

    return {
        "symbol":          symbol,
        "best_strategy":   best,
        "best_score":      best_score,
        "candidates":      candidates,
        "market_bias":     market_bias,
        "market_state":    market_state,
        "spot":            round(spot, 2),
        "expiry":          expiry,
        "all_expiries":    chain.get("all_expiries", []),
        "bull_score":      dec.get("bull_score", 0),
        "bear_score":      dec.get("bear_score", 0),
        "confidence":      dec.get("confidence", 0),
        "signal_strength": signal_strength,
        "bullish_probability": dec.get("bullish_probability", 50),
        "bearish_probability": dec.get("bearish_probability", 50),
        "max_pain":        max_pain,
        "pcr":             market_data.get("pcr", 0),
        "vix":             vix,
        "expected_move":   exp_move,
        "strategy":        dec.get("strategy", ""),
        "strategy_reason": dec.get("strategy_reason", ""),
        "strikes":         strikes,
        "price_levels":    price_levels,
        "strategy_detail": strategy_detail,
        "time_session":    time_info.get("session", ""),
        "expiry_info":     expiry_info,
        "iv_info":         iv_info,
        "warnings":        warnings,
        "wait_reasons":    [],
        "signal_history":  get_history(symbol),
        "signal_reversal": sig.get("reversal", False),
        "reversal_type":   sig.get("reversal_type", ""),
        "ai_reason":       ai_reason,
        "disclaimer":      "Not investment advice. Trade at your own risk.",
        # ── V2 fields ─────────────────────────────────────────────────────
        "confluence":        confluence,
        "market_regime":     regime,
        "action":            action_label,
        "v2_trade_levels":   v2_trade_levels,
        "v2_risk":           v2_risk,
        "win_probability":   None,
        "win_probability_note": "Win Probability: Not enough historical data",
    }


@router.get("/history/{symbol}")
async def signal_history(symbol: str):
    """Last 20 strategy signals for a symbol."""
    return {
        "symbol":  symbol,
        "history": get_history(symbol.upper()),
        "count":   len(get_history(symbol.upper())),
    }
