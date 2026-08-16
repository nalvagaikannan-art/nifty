"""
Rule-Based Decision Engine
===========================
20 conditions score செய்து Bull/Bear/Neutral score கணக்கிடும்.
AI இந்த score-ஐ verify செய்து reasoning மட்டும் சொல்லும்.
"""

from typing import Dict, List, Tuple
import logging

from app.services.market_regime import classify_market_regime

logger = logging.getLogger(__name__)

# ── Score weights ─────────────────────────────────────────────────────────
# india_vix and atr_risk are intentionally 0: VIX and ATR describe how MUCH
# the market might move, not which WAY — using them as bull/bear points
# (low VIX/ATR = "bullish") was mislabeling a volatility reading as a
# directional one. They're still computed and shown (see _score_vix/_score_atr
# below), and they now drive `volatility_regime` / confidence damping instead
# of bull_score/bear_score. max_possible (used to normalise confidence) is
# derived from this dict, so zeroing them here also correctly removes them
# from that denominator instead of leaving unearnable weight in it.
WEIGHTS = {
    "pcr":             6,
    "oi_change":       6,
    "max_pain":        5,
    "call_writing":    5,
    "put_writing":     5,
    "futures_premium": 4,
    "vwap":            4,
    "ema20":           5,
    "ema50":           5,
    "rsi":             5,
    "macd":            5,
    "adx":             4,
    "atr_risk":        0,
    "supertrend":      5,
    "volume_spike":    4,
    "india_vix":       0,
    "global_market":   4,
    "gift_nifty":      5,
    "fii":             4,
    "dii":             4,
}

# ── Correlation buckets (review #7) ─────────────────────────────────────────
# vwap/ema20/ema50/macd/adx/supertrend/rsi are 7 different lenses on the SAME
# underlying fact — "is price trending up or down right now" — so a single
# trend day can independently trip 6-7 of them in the same direction, and
# summing full weight for each let one real signal masquerade as seven,
# inflating both bull/bear score AND the confidence derived from it.
# pcr/oi_change/max_pain/call_writing/put_writing are similarly all reading
# the SAME option-chain positioning from different angles.
#
# Fix: indicators in the same bucket that agree on direction no longer each
# get full weight. The single strongest agreeing indicator in a bucket
# counts fully; each additional agreeing indicator in that bucket counts at
# a shrinking fraction (diminishing returns), reflecting that it's mostly
# confirming the same underlying fact rather than adding independent
# evidence. Indicators in DIFFERENT buckets still add fully — genuine
# independent evidence (e.g. trend + options-flow + FII agreeing) should
# still combine, that part of the review's ask was correct as a goal.
INDICATOR_BUCKET = {
    "pcr": "options_flow", "oi_change": "options_flow", "max_pain": "options_flow",
    "call_writing": "options_flow", "put_writing": "options_flow",
    "vwap": "trend", "ema20": "trend", "ema50": "trend", "macd": "trend",
    "adx": "trend", "supertrend": "trend", "rsi": "trend",
    "futures_premium": "flow_global", "global_market": "flow_global",
    "gift_nifty": "flow_global", "fii": "flow_global", "dii": "flow_global",
    "volume_spike": "volume",
    "atr_risk": "volatility_context", "india_vix": "volatility_context",
}
# 1.0, 0.55, 0.35, 0.22, 0.15, 0.10, 0.08 — first agreeing indicator in a
# bucket keeps full weight, each further one adds a shrinking amount instead
# of another full share.
BUCKET_DIMINISHING = [1.0, 0.55, 0.35, 0.22, 0.15, 0.10, 0.08]


def _max_dampened_score() -> float:
    """Theoretical max bull (or bear) score if every indicator in every
    bucket pointed the same direction at full weight, run through the same
    dampening as _dampened_bull_bear. Used to normalise `confidence` against
    what's actually achievable post-dampening — reusing the old raw
    sum(WEIGHTS.values()) here would systematically under-read confidence
    now that correlated indicators no longer stack at full weight each."""
    per_bucket: Dict[str, List[int]] = {}
    for name, weight in WEIGHTS.items():
        if weight <= 0:
            continue
        bucket = INDICATOR_BUCKET.get(name, name)
        per_bucket.setdefault(bucket, []).append(weight)
    total = 0.0
    for weights in per_bucket.values():
        weights.sort(reverse=True)
        for i, w in enumerate(weights):
            factor = BUCKET_DIMINISHING[i] if i < len(BUCKET_DIMINISHING) else BUCKET_DIMINISHING[-1]
            total += w * factor
    return total


MAX_DAMPENED_SCORE = _max_dampened_score()


# ── Market-regime-adaptive weighting (review #4) ────────────────────────────
# market_regime.py already classifies TREND_UP/TREND_DOWN/RANGE/BREAKOUT/
# BREAKDOWN/HIGH_VOLATILITY/LOW_VOLATILITY/EXPIRY_HIGH_GAMMA/NO_TRADE from
# the same market_data this engine already has — it just wasn't connected
# to the scoring here (only to the separate /strategy route). Reviewer's
# concrete ask: "Trending market → trend indicators high weight, options
# flow lower; Sideways → OI/PCR high weight, trend lower; High VIX → confidence
# reduced." Implemented as a per-bucket multiplier applied to each
# indicator's points BEFORE the correlation-dampening step above — a
# TREND_UP day's trend-bucket evidence counts for more, a RANGE day's
# options-flow-bucket evidence counts for more, rather than every regime
# treating all 20 conditions identically.
REGIME_BUCKET_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    "TREND_UP":          {"trend": 1.3,  "options_flow": 0.8,  "flow_global": 1.0, "volume": 1.15},
    "TREND_DOWN":        {"trend": 1.3,  "options_flow": 0.8,  "flow_global": 1.0, "volume": 1.15},
    "RANGE":             {"trend": 0.65, "options_flow": 1.3,  "flow_global": 0.9, "volume": 0.9},
    "BREAKOUT":          {"trend": 1.2,  "options_flow": 1.0,  "flow_global": 1.0, "volume": 1.4},
    "BREAKDOWN":         {"trend": 1.2,  "options_flow": 1.0,  "flow_global": 1.0, "volume": 1.4},
    "HIGH_VOLATILITY":   {"trend": 0.85, "options_flow": 1.1,  "flow_global": 0.9, "volume": 1.0},
    "LOW_VOLATILITY":    {"trend": 1.0,  "options_flow": 1.0,  "flow_global": 1.0, "volume": 0.9},
    "EXPIRY_HIGH_GAMMA":  {"trend": 0.85, "options_flow": 1.2, "flow_global": 0.9, "volume": 1.0},
    # NO_TRADE isn't in here on purpose — handled as a hard gate below
    # (forces NONE/Sideways outright) rather than a soft reweight.
}
# Regimes where the market itself is unusually risky/uncertain get an
# additional flat confidence cut on top of the bucket reweighting — the
# review's explicit "High VIX → Signal confidence Reduced" ask.
REGIME_CONFIDENCE_MULTIPLIERS: Dict[str, float] = {
    "HIGH_VOLATILITY":   0.80,
    "EXPIRY_HIGH_GAMMA":  0.85,
}


def _apply_regime_weighting(items: List[Tuple[str, str, int]], regime: str) -> List[Tuple[str, str, float]]:
    mult = REGIME_BUCKET_MULTIPLIERS.get(regime, {})
    if not mult:
        return items
    out = []
    for name, direction, pts in items:
        bucket = INDICATOR_BUCKET.get(name, name)
        out.append((name, direction, pts * mult.get(bucket, 1.0)))
    return out


def _dampened_bull_bear(items: List[Tuple[str, str, int]]) -> Tuple[int, int]:
    """items: list of (indicator_name, direction, points) already recorded.
    Returns (bull, bear) totals with same-bucket, same-direction contributions
    diminished per BUCKET_DIMINISHING instead of summed at full weight."""
    buckets: Dict[Tuple[str, str], List[int]] = {}
    for name, direction, pts in items:
        if direction not in ("bull", "bear") or pts <= 0:
            continue
        bucket = INDICATOR_BUCKET.get(name, name)  # unbucketed indicators stand alone
        buckets.setdefault((bucket, direction), []).append(pts)

    bull = bear = 0
    for (bucket, direction), pts_list in buckets.items():
        pts_list.sort(reverse=True)
        total = 0
        for i, pts in enumerate(pts_list):
            factor = BUCKET_DIMINISHING[i] if i < len(BUCKET_DIMINISHING) else BUCKET_DIMINISHING[-1]
            total += pts * factor
        total = round(total)
        if direction == "bull":
            bull += total
        else:
            bear += total
    return bull, bear


def _score_pcr(pcr: float) -> Tuple[str, int, str]:
    """PCR > 1.2 = Bullish (put writing heavy), < 0.8 = Bearish.
    FIX: real bug — pcr=0.0 (option_analyzer.compute_pcr's sentinel for an
    empty/unfetchable chain, same 0-as-"no data" pattern as everywhere
    else in this file) fell straight into the `pcr <= 0.7` branch below
    with FULL bear weight, i.e. a completely missing option chain was
    scored as the STRONGEST possible bearish signal. A real PCR is always
    > 0 (can't have zero OI on both sides of a live chain), so pcr<=0 is
    an unambiguous sentinel, not a genuine reading."""
    if pcr <= 0:
        return "neutral", 0, "PCR unavailable (option chain data missing)"
    if pcr >= 1.3:
        return "bull", WEIGHTS["pcr"], f"PCR {pcr:.2f} — Strong put writing, bullish"
    elif pcr >= 1.1:
        return "bull", int(WEIGHTS["pcr"] * 0.6), f"PCR {pcr:.2f} — Mild put writing"
    elif pcr <= 0.7:
        return "bear", WEIGHTS["pcr"], f"PCR {pcr:.2f} — Strong call writing, bearish"
    elif pcr <= 0.9:
        return "bear", int(WEIGHTS["pcr"] * 0.6), f"PCR {pcr:.2f} — Mild call writing"
    else:
        return "neutral", 0, f"PCR {pcr:.2f} — Neutral zone"


def _score_oi_change(oi: Dict) -> Tuple[str, int, str]:
    ce_chg = oi.get("ce_change", 0)
    pe_chg = oi.get("pe_change", 0)
    if pe_chg > ce_chg and pe_chg > 0:
        return "bull", WEIGHTS["oi_change"], f"PE OI buildup (+{pe_chg:,}) > CE — bullish"
    elif ce_chg > pe_chg and ce_chg > 0:
        return "bear", WEIGHTS["oi_change"], f"CE OI buildup (+{ce_chg:,}) > PE — bearish"
    return "neutral", 0, "OI change neutral"


def _score_max_pain(spot: float, max_pain: float) -> Tuple[str, int, str]:
    if max_pain <= 0:
        return "neutral", 0, "Max Pain data unavailable"
    diff_pct = ((spot - max_pain) / max_pain) * 100
    if diff_pct > 1.5:
        return "bear", WEIGHTS["max_pain"], f"Spot {spot:.0f} >> Max Pain {max_pain:.0f} (+{diff_pct:.1f}%) — gravity pull down"
    elif diff_pct < -1.5:
        return "bull", WEIGHTS["max_pain"], f"Spot {spot:.0f} << Max Pain {max_pain:.0f} ({diff_pct:.1f}%) — gravity pull up"
    return "neutral", int(WEIGHTS["max_pain"] * 0.3), f"Spot near Max Pain {max_pain:.0f}"


def _score_call_writing(df_summary: Dict) -> Tuple[str, int, str]:
    """Heavy call writing at ATM+1 strikes = resistance = bearish"""
    ce_top_oi = df_summary.get("ce_max_oi_strike", 0)
    spot = df_summary.get("spot", 0)
    if ce_top_oi and spot:
        dist = ce_top_oi - spot
        if 0 < dist < spot * 0.02:  # within 2% above spot
            return "bear", WEIGHTS["call_writing"], f"Heavy Call writing at {ce_top_oi:.0f} — near resistance"
        elif dist > spot * 0.03:
            return "bull", int(WEIGHTS["call_writing"] * 0.5), f"Call writing far OTM at {ce_top_oi:.0f} — less resistance"
    return "neutral", 0, "Call writing pattern neutral"


def _score_put_writing(df_summary: Dict) -> Tuple[str, int, str]:
    """Heavy put writing below spot = support = bullish"""
    pe_top_oi = df_summary.get("pe_max_oi_strike", 0)
    spot = df_summary.get("spot", 0)
    if pe_top_oi and spot:
        dist = spot - pe_top_oi
        if 0 < dist < spot * 0.02:
            return "bull", WEIGHTS["put_writing"], f"Heavy Put writing at {pe_top_oi:.0f} — strong support"
        elif dist > spot * 0.03:
            return "bear", int(WEIGHTS["put_writing"] * 0.5), f"Put writing far OTM at {pe_top_oi:.0f} — weak support"
    return "neutral", 0, "Put writing pattern neutral"


def _score_futures_premium(premium: float, status: str = "live") -> Tuple[str, int, str]:
    if status != "live":
        return "neutral", 0, "Futures premium unavailable (Angel One not connected) — not treated as neutral"
    if premium > 30:
        return "bull", WEIGHTS["futures_premium"], f"Futures premium +{premium:.0f} — strong long buildup"
    elif premium > 10:
        return "bull", int(WEIGHTS["futures_premium"] * 0.5), f"Futures premium +{premium:.0f} — mild bullish"
    elif premium < -30:
        return "bear", WEIGHTS["futures_premium"], f"Futures discount {premium:.0f} — short buildup"
    elif premium < -10:
        return "bear", int(WEIGHTS["futures_premium"] * 0.5), f"Futures discount {premium:.0f} — mild bearish"
    return "neutral", 0, f"Futures premium {premium:.0f} — neutral (live)"


def _score_vwap(spot: float, vwap: float) -> Tuple[str, int, str]:
    if vwap <= 0:
        return "neutral", 0, "VWAP unavailable"
    if spot > vwap * 1.003:
        return "bull", WEIGHTS["vwap"], f"Spot {spot:.0f} above VWAP {vwap:.0f} — bullish"
    elif spot < vwap * 0.997:
        return "bear", WEIGHTS["vwap"], f"Spot {spot:.0f} below VWAP {vwap:.0f} — bearish"
    return "neutral", 0, f"Spot near VWAP {vwap:.0f}"


def _score_ema(spot: float, ema20: float, ema50: float) -> Tuple[Tuple, Tuple]:
    if ema20 > 0:
        if spot > ema20 * 1.002:
            s20 = ("bull", WEIGHTS["ema20"], f"Above EMA20 {ema20:.0f} — bullish")
        elif spot < ema20 * 0.998:
            s20 = ("bear", WEIGHTS["ema20"], f"Below EMA20 {ema20:.0f} — bearish")
        else:
            s20 = ("neutral", 0, f"Near EMA20 {ema20:.0f}")
    else:
        s20 = ("neutral", 0, "EMA20 unavailable")

    if ema50 > 0:
        if spot > ema50 * 1.002:
            s50 = ("bull", WEIGHTS["ema50"], f"Above EMA50 {ema50:.0f} — bullish")
        elif spot < ema50 * 0.998:
            s50 = ("bear", WEIGHTS["ema50"], f"Below EMA50 {ema50:.0f} — bearish")
        else:
            s50 = ("neutral", 0, f"Near EMA50 {ema50:.0f}")
    else:
        s50 = ("neutral", 0, "EMA50 unavailable")

    return s20, s50


def _score_rsi(rsi: float) -> Tuple[str, int, str]:
    if rsi >= 70:
        return "bear", WEIGHTS["rsi"], f"RSI {rsi:.1f} — Overbought, caution"
    elif rsi >= 60:
        return "bull", int(WEIGHTS["rsi"] * 0.7), f"RSI {rsi:.1f} — Bullish momentum"
    elif rsi <= 30:
        return "bull", WEIGHTS["rsi"], f"RSI {rsi:.1f} — Oversold, bounce possible"
    elif rsi <= 40:
        return "bear", int(WEIGHTS["rsi"] * 0.7), f"RSI {rsi:.1f} — Bearish momentum"
    return "neutral", 0, f"RSI {rsi:.1f} — Neutral"


def _score_macd(macd: Dict) -> Tuple[str, int, str]:
    m = macd.get("macd", 0)
    s = macd.get("signal", 0)
    h = macd.get("histogram", 0)
    if m > s and h > 0:
        return "bull", WEIGHTS["macd"], f"MACD {m:.1f} > Signal {s:.1f} — Bullish crossover"
    elif m < s and h < 0:
        return "bear", WEIGHTS["macd"], f"MACD {m:.1f} < Signal {s:.1f} — Bearish crossover"
    return "neutral", 0, f"MACD {m:.1f} — No clear crossover"


def _score_adx(adx: float, di_plus: float, di_minus: float) -> Tuple[str, int, str]:
    if adx <= 0:
        return "neutral", 0, "ADX unavailable"
    if adx < 20:
        return "neutral", 0, f"ADX {adx:.1f} — No trend (range bound)"
    if di_plus > di_minus:
        return "bull", WEIGHTS["adx"], f"ADX {adx:.1f}, +DI {di_plus:.1f} > -DI {di_minus:.1f} — Strong uptrend"
    else:
        return "bear", WEIGHTS["adx"], f"ADX {adx:.1f}, -DI {di_minus:.1f} > +DI {di_plus:.1f} — Strong downtrend"


def _score_atr(atr: float, spot: float) -> Tuple[str, int, str]:
    """ATR is a RANGE/RISK measure, not a direction. Low ATR does not mean
    bullish — it means the market is quiet. Always neutral/0 points here;
    see `_volatility_context()` for how ATR feeds expected-move and
    confidence instead of bull/bear score."""
    if atr <= 0 or spot <= 0:
        return "neutral", 0, "ATR unavailable"
    atr_pct = (atr / spot) * 100
    if atr_pct > 1.5:
        return "neutral", 0, f"ATR {atr:.0f} ({atr_pct:.1f}%) — High volatility, wider range expected (not directional)"
    return "neutral", 0, f"ATR {atr:.0f} ({atr_pct:.1f}%) — Low volatility, narrower range expected (not directional)"


def _score_supertrend(supertrend: str) -> Tuple[str, int, str]:
    st = (supertrend or "").lower()
    if st == "buy":
        return "bull", WEIGHTS["supertrend"], "Supertrend: BUY signal"
    elif st == "sell":
        return "bear", WEIGHTS["supertrend"], "Supertrend: SELL signal"
    return "neutral", 0, "Supertrend: Neutral"


def _score_volume(volume_spike: bool, volume_ratio: float) -> Tuple[str, int, str]:
    if volume_spike and volume_ratio > 1.5:
        return "bull", WEIGHTS["volume_spike"], f"Volume spike {volume_ratio:.1f}x avg — strong interest"
    elif volume_spike:
        return "bull", int(WEIGHTS["volume_spike"] * 0.5), f"Volume slightly elevated {volume_ratio:.1f}x"
    return "neutral", 0, "Volume normal"


def _score_vix(vix: float) -> Tuple[str, int, str]:
    """India VIX measures expected volatility, not direction — a low VIX
    does not mean bullish and a high VIX does not mean bearish (a bullish
    breakout can happen WITH rising VIX). Always neutral/0 points; see
    `_volatility_context()` for how VIX feeds the volatility regime and
    confidence damping instead."""
    if vix <= 0:
        return "neutral", 0, "India VIX unavailable"
    if vix > 22:
        return "neutral", 0, f"India VIX {vix:.1f} — High volatility regime (not directional)"
    elif vix < 12:
        return "neutral", 0, f"India VIX {vix:.1f} — Low volatility regime (not directional)"
    return "neutral", 0, f"India VIX {vix:.1f} — Moderate volatility regime"


def _score_global(global_pct: float, status: str = "live") -> Tuple[str, int, str]:
    if status != "live":
        return "neutral", 0, "Global market cues unavailable — not treated as flat"
    if global_pct > 0.5:
        return "bull", WEIGHTS["global_market"], f"Global markets +{global_pct:.1f}% — positive"
    elif global_pct < -0.5:
        return "bear", WEIGHTS["global_market"], f"Global markets {global_pct:.1f}% — negative"
    return "neutral", 0, f"Global markets {global_pct:.1f}% — flat (live)"


def _score_gift(gift_pct: float, status: str = "live") -> Tuple[str, int, str]:
    if status != "live":
        return "neutral", 0, "Gift Nifty data unavailable — not treated as flat open"
    if gift_pct > 0.3:
        return "bull", WEIGHTS["gift_nifty"], f"Gift Nifty +{gift_pct:.1f}% — gap-up expected"
    elif gift_pct < -0.3:
        return "bear", WEIGHTS["gift_nifty"], f"Gift Nifty {gift_pct:.1f}% — gap-down expected"
    return "neutral", 0, f"Gift Nifty {gift_pct:.1f}% — flat open (live)"


def _score_fii(fii_net: float, status: str = "live") -> Tuple[str, int, str]:
    if status != "live":
        return "neutral", 0, "FII data unavailable"
    if fii_net > 500:
        return "bull", WEIGHTS["fii"], f"FII net buy ₹{fii_net:.0f}Cr — strong inflow"
    elif fii_net > 0:
        return "bull", int(WEIGHTS["fii"] * 0.5), f"FII net buy ₹{fii_net:.0f}Cr"
    elif fii_net < -500:
        return "bear", WEIGHTS["fii"], f"FII net sell ₹{fii_net:.0f}Cr — strong outflow"
    elif fii_net < 0:
        return "bear", int(WEIGHTS["fii"] * 0.5), f"FII net sell ₹{fii_net:.0f}Cr"
    return "neutral", 0, "FII net flow ~0 (live)"


def _score_dii(dii_net: float, status: str = "live") -> Tuple[str, int, str]:
    if status != "live":
        return "neutral", 0, "DII data unavailable"
    if dii_net > 500:
        return "bull", WEIGHTS["dii"], f"DII net buy ₹{dii_net:.0f}Cr — domestic support"
    elif dii_net > 0:
        return "bull", int(WEIGHTS["dii"] * 0.5), f"DII net buy ₹{dii_net:.0f}Cr"
    elif dii_net < -500:
        return "bear", WEIGHTS["dii"], f"DII net sell ₹{dii_net:.0f}Cr"
    return "neutral", 0, "DII net flow ~0 (live)"


def _volatility_context(vix: float, atr_pct: float) -> Dict:
    """VIX + ATR combined into a single, honestly-labelled volatility
    regime (never bull/bear) plus a rough expected-move band, used to:
      - dampen `confidence` when volatility is high and trend is weak
        (a wide-range environment deserves LESS certainty, not more)
      - drive the 'Signal Strength, not probability' framing

    FIX: previously only checked `vix <= 0 AND atr_pct <= 0` before
    treating the regime as unknown — if just ONE of the two was actually
    unavailable (e.g. VIX fetch failed but ATR had data), that single
    missing 0 still fed into `score` as real evidence (vix=0 < 12 → scored
    as "low volatility", exactly the "0 read as real data" bug flagged in
    review). Each term below now only contributes if ITS OWN source had
    data — a missing component is dropped from the score, not defaulted to
    a directional-sounding zero, and the label says which parts were live.
    """
    vix_available = vix > 0
    atr_available = atr_pct > 0
    if not vix_available and not atr_available:
        return {"regime": "unknown", "label": "Volatility data unavailable"}

    score = 0
    if vix_available:
        if vix > 22:
            score += 2
        elif vix < 12:
            score -= 1
    if atr_available:
        if atr_pct > 1.5:
            score += 2
        elif atr_pct < 0.5:
            score -= 1

    vix_txt = f"VIX {vix:.1f}" if vix_available else "VIX N/A"
    atr_txt = f"ATR {atr_pct:.1f}%" if atr_available else "ATR N/A"
    if score >= 3:
        regime, label = "high", f"High volatility ({vix_txt}, {atr_txt}) — big moves possible, either direction"
    elif score <= -1:
        regime, label = "low", f"Low volatility ({vix_txt}, {atr_txt}) — quiet, narrow-range market"
    else:
        regime, label = "normal", f"Normal volatility ({vix_txt}, {atr_txt})"
    if not (vix_available and atr_available):
        label += " ⚠️ partial data"
    return {"regime": regime, "label": label}


# ── Option Strategy Suggestion ────────────────────────────────────────────
def _suggest_strategy(
    preferred_side: str, margin: float, adx: float, vix: float, atr_pct: float
) -> Tuple[str, str]:
    """
    "Sideways/No clear edge" ஒரு dead-end message-ஆ இருக்காம, trend
    strength (ADX) + volatility (VIX/ATR) வைத்து ஒரு actionable option
    strategy category பரிந்துரைக்கும்.

    preferred_side: "CALL", "PUT", or "NONE" (from run_decision_engine's
    bias score — NOT a buy/sell instruction, just which side the score
    currently favours).

    ⚠️ Rule-based hint மட்டும் — investment advice இல்லை. இது ஒரு strategy
    *category* மட்டும் suggest பண்ணும், order எதுவும் place ஆகாது.
    """
    trending = adx >= 20
    high_vol = vix >= 20 or atr_pct >= 1.2

    if preferred_side == "CALL" and trending:
        return (
            "Directional Call Bias",
            f"Trend strong (ADX {adx:.1f}), bull margin {margin:+.0f} — "
            f"ATM/ITM CE ஒரு consideration, trend continuation எதிர்பார்க்கலாம்."
        )

    if preferred_side == "PUT" and trending:
        return (
            "Directional Put Bias",
            f"Trend strong (ADX {adx:.1f}), bear margin {margin:+.0f} — "
            f"ATM/ITM PE ஒரு consideration, trend continuation எதிர்பார்க்கலாம்."
        )

    if preferred_side in ("CALL", "PUT") and not trending:
        opt = "CE" if preferred_side == "CALL" else "PE"
        return (
            f"Weak-Trend {opt} Bias — Small Size",
            f"Direction bias இருக்கு (margin {margin:+.0f}) ஆனா ADX {adx:.1f} < 20 "
            f"(trend weak/range-bound) — full-size conviction குறைவு. Small qty {opt} "
            f"consideration அல்லது confirmation candle வரைக்கும் காத்திருக்கலாம்."
        )

    if high_vol and not trending:
        return (
            "Long Straddle / Strangle",
            f"High volatility (VIX {vix:.1f}, ATR {atr_pct:.1f}%) ஆனா clear direction "
            f"இல்ல — பெரிய move எதிர்பார்க்கலாம் ஆனா எந்த side-ன்னு certain இல்ல. "
            f"IV ஏற்கனவே high-ஆ இருந்தா premium costly-ஆ இருக்கும், கவனமா இருங்க."
        )

    if not trending and not high_vol:
        return (
            "Range Strategy — Non-Directional",
            f"ADX {adx:.1f} < 20 (range-bound), VIX {vix:.1f} moderate/low — market "
            f"sideways-ஆ இருக்கு. Naked directional buying-க்கு clear edge இல்ல; "
            f"theta-decay favour பண்ண Iron Condor/Short Strangle (premium selling) "
            f"ஒரு consideration, அல்லது range breakout-க்கு காத்திருக்கலாம்."
        )

    return (
        "No Clear Edge — Sideways",
        f"Bull/Bear score close-ஆ இருக்கு (margin {margin:+.0f}), trend/volatility "
        f"signals mixed. தெளிவான confirmation வரும் வரைக்கும் fresh position "
        f"தவிர்ப்பது safe."
    )


# ── Scenarios + invalidation ───────────────────────────────────────────────
def _build_scenarios(
    spot: float, sr: Dict, bull_prob: int, bear_prob: int, margin: float
) -> List[Dict]:
    """
    3 explicit scenarios (upside / downside / range) each with a trigger
    condition, a target zone, and an INVALIDATION level — what would prove
    that scenario wrong. Built only from levels this app already computed
    (support/resistance — see technical_indicators.combine_support_resistance),
    never invented numbers.

    bull_prob/bear_prob (from the 20-condition score, always summing to 100)
    describe direction IF the market moves — they say nothing about whether
    it moves at all. Sideways probability is estimated separately from how
    close the bull/bear tally is (small margin → more likely range-bound),
    then bull/bear are rescaled into the remainder so all three sum to 100.
    """
    if spot <= 0:
        return []
    resistance = sorted(sr.get("resistance", []))
    support    = sorted(sr.get("support", []), reverse=True)
    r1 = resistance[0] if resistance else round(spot * 1.005, 0)
    r2 = resistance[1] if len(resistance) > 1 else round(spot * 1.01, 0)
    s1 = support[0] if support else round(spot * 0.995, 0)
    s2 = support[1] if len(support) > 1 else round(spot * 0.99, 0)

    sideways_prob = max(10, min(60, 40 - abs(margin)))
    remainder = 100 - sideways_prob
    bull_scenario_prob = round(remainder * bull_prob / 100)
    bear_scenario_prob = remainder - bull_scenario_prob

    return [
        {
            "type": "bullish",
            "probability": bull_scenario_prob,
            "condition": f"{r1:,.0f} resistance-ஐ volume-உடன் break செய்தால்",
            "target_zone": f"{r1:,.0f} – {r2:,.0f}",
            "invalidation": f"{s1:,.0f}-க்கு கீழே sustained trade ஆனால் இந்த bullish view தவறு",
        },
        {
            "type": "bearish",
            "probability": bear_scenario_prob,
            "condition": f"{s1:,.0f} support கீழே sustained trade ஏற்பட்டால்",
            "target_zone": f"{s2:,.0f} – {s1:,.0f}",
            "invalidation": f"{r1:,.0f}-க்கு மேலே reclaim ஆனால் இந்த bearish view தவறு",
        },
        {
            "type": "sideways",
            "probability": round(sideways_prob),
            "condition": f"{s1:,.0f} – {r1:,.0f} range-க்குள் trade தொடர்ந்தால்",
            "target_zone": f"{s1:,.0f} – {r1:,.0f}",
            "invalidation": f"இந்த range-ஐ இரு பக்கமும் decisively break செய்தால் range view தவறு",
        },
    ]


# ── Data-availability tracking (review: "Data இல்லை → 0 → bullish/bearish
# calculation ஆகக் கூடாது") ─────────────────────────────────────────────────
# Every _score_* function above already treats missing data as neutral/0
# points — that part was already correct going in. What was still missing:
# confidence itself didn't know WHY an indicator went neutral. A genuinely
# balanced/no-signal reading and a "we have no data at all" reading both
# produced identical neutral/0 output, so confidence was computed only from
# whatever weight WAS available — a signal built on 6 live indicators out of
# 20 could still show high confidence, with no visible penalty for the other
# 14 being blind. This maps each indicator to whether its underlying data
# was actually available (independent of what direction it scored), and
# dampens confidence by how much of the total possible weight was missing.
def _data_availability(market_data: Dict, tech_src: str) -> Dict[str, bool]:
    # PCR/OI-change/max-pain/writing patterns all derive from the same
    # option-chain fetch — pcr<=0 is that fetch's own "empty/unavailable"
    # sentinel (see _score_pcr fix above), so it's a more direct signal
    # than the oi_summary truthiness check used before.
    chain_available = market_data.get("pcr", 0) > 0
    # tech_src == "placeholder" means self.tech._empty() was used for ALL of
    # these at once (see market_analyzer.py) — one flag covers the whole group.
    tech_available = tech_src != "placeholder"
    return {
        "pcr": chain_available, "oi_change": chain_available, "max_pain": chain_available,
        "call_writing": chain_available, "put_writing": chain_available,
        "futures_premium": market_data.get("futures_premium_status") == "live",
        "vwap": tech_available, "ema20": tech_available, "ema50": tech_available,
        "rsi": tech_available, "macd": tech_available, "adx": tech_available,
        "supertrend": tech_available, "volume_spike": tech_available,
        "global_market": market_data.get("global_status") == "live",
        "gift_nifty": market_data.get("gift_status") == "live",
        "fii": market_data.get("fii_status") == "live",
        "dii": market_data.get("dii_status") == "live",
        # atr_risk/india_vix weigh 0 already — availability doesn't affect
        # confidence math, but tracked for the data_completeness_pct display.
        "atr_risk": tech_available, "india_vix": True,
    }


# ── Main Engine ──────────────────────────────────────────────────────────────

def run_decision_engine(market_data: Dict) -> Dict:
    """
    20 conditions score செய்து Bull/Bear/Neutral score return செய்யும்.
    Output: decision, bull_score, bear_score, neutral_score,
            confidence, risk, reasons[]
    """
    spot_data  = market_data.get("spot", {})
    spot       = spot_data.get("price", 0)
    technicals = market_data.get("technicals", {})
    oi_summary = market_data.get("oi_summary", {})
    oi_summary["spot"] = spot

    scores = {"bull": 0, "bear": 0, "neutral": 0}
    reasons: List[str] = []
    recorded_items: List[Tuple[str, str, int]] = []  # (indicator_name, direction, points) — for bucket dampening

    def record(name: str, result: Tuple):
        direction, pts, reason = result
        scores[direction] = scores.get(direction, 0) + pts
        recorded_items.append((name, direction, pts))
        reasons.append(f"{'🟢' if direction=='bull' else '🔴' if direction=='bear' else '⚪'} {reason}")

    # 1. PCR
    record("pcr", _score_pcr(market_data.get("pcr", 0)))
    # 2. OI Change
    record("oi_change", _score_oi_change(market_data.get("oi_change", {})))
    # 3. Max Pain
    record("max_pain", _score_max_pain(spot, market_data.get("max_pain", 0)))
    # 4. Call Writing
    record("call_writing", _score_call_writing(oi_summary))
    # 5. Put Writing
    record("put_writing", _score_put_writing(oi_summary))
    # 6. Futures Premium
    record("futures_premium", _score_futures_premium(
        market_data.get("futures_premium", 0),
        market_data.get("futures_premium_status", "live"),
    ))
    # 7. VWAP
    record("vwap", _score_vwap(spot, technicals.get("vwap", 0)))
    # 8+9. EMA20, EMA50
    s20, s50 = _score_ema(spot, technicals.get("ema20", 0), technicals.get("ema50", 0))
    record("ema20", s20); record("ema50", s50)
    # 10. RSI
    record("rsi", _score_rsi(market_data.get("rsi", 50)))
    # 11. MACD
    record("macd", _score_macd(market_data.get("macd", {})))
    # 12. ADX
    record("adx", _score_adx(technicals.get("adx", 0), technicals.get("di_plus", 0), technicals.get("di_minus", 0)))
    # 13. ATR Risk
    record("atr_risk", _score_atr(technicals.get("atr", 0), spot))
    # 14. Supertrend
    record("supertrend", _score_supertrend(technicals.get("supertrend", "")))
    # 15. Volume Spike
    record("volume_spike", _score_volume(technicals.get("volume_spike", False), technicals.get("volume_ratio", 1.0)))
    # 16. India VIX
    record("india_vix", _score_vix(market_data.get("vix", 15)))
    # 17. Global Market
    record("global_market", _score_global(
        market_data.get("global_change_pct", 0), market_data.get("global_status", "live")
    ))
    # 18. Gift Nifty
    record("gift_nifty", _score_gift(
        market_data.get("gift_nifty_change_pct", 0), market_data.get("gift_status", "live")
    ))
    # 19. FII
    record("fii", _score_fii(
        market_data.get("fii_net_cr", 0), market_data.get("fii_status", "live")
    ))
    # 20. DII
    record("dii", _score_dii(
        market_data.get("dii_net_cr", 0), market_data.get("dii_status", "live")
    ))

    # Review #4: classify market regime from the SAME market_data (reusing
    # market_regime.py, previously wired only into the /strategy route) and
    # reweight each recorded indicator by its bucket BEFORE dampening — a
    # TREND_UP day's trend evidence counts more, a RANGE day's options-flow
    # evidence counts more. Never lets classify_market_regime() itself raise
    # into the main scoring path — a regime-classification bug should
    # degrade to "no reweighting", not take down signal generation.
    try:
        regime_info = classify_market_regime(market_data)
    except Exception:
        logger.exception("Market regime classification failed — proceeding without regime reweighting")
        regime_info = {"regime": "RANGE", "confidence": "LOW", "no_trade": False, "reasons": []}
    regime = regime_info.get("regime", "RANGE")
    weighted_items = _apply_regime_weighting(recorded_items, regime)

    # Review #7: raw scores["bull"]/["bear"] are the OLD sum-everything
    # totals (kept only for the `raw_bull_score`/`raw_bear_score` debug
    # fields below); the bucket-dampened totals are what actually drive the
    # bias/probability/confidence from here on, so correlated indicators
    # (7 different trend readings, 5 different options-flow readings) no
    # longer multiply a single real signal into an outsized score.
    bull, bear = _dampened_bull_bear(weighted_items)

    # ── Bias / probability (replaces the old BUY CALL / BUY PUT / WAIT /
    # NO TRADE decision string) ─────────────────────────────────────────
    # This app shows analysis only — a market-direction read and its
    # confidence, never a buy/sell instruction. bullish/bearish_probability
    # are the bull/bear scores normalised to sum to 100, i.e. a direct,
    # transparent read of the same 20-condition scoring below — not a
    # separately-fit statistical model.
    margin = bull - bear
    bull_bear_total = bull + bear
    if bull_bear_total > 0:
        bullish_probability = round(bull / bull_bear_total * 100)
    else:
        bullish_probability = 50
    bearish_probability = 100 - bullish_probability

    if margin >= 15:
        market_bias = "Bullish"
    elif margin <= -15:
        market_bias = "Bearish"
    elif abs(margin) < 8:
        market_bias = "Sideways"
    else:
        market_bias = "Bullish" if margin > 0 else "Bearish"  # leaning, lower confidence below reflects the uncertainty

    if margin >= 10:
        preferred_side = "CALL"
        risk = "Low" if bull >= 40 else "Medium"
    elif margin <= -10:
        preferred_side = "PUT"
        risk = "Low" if bear >= 40 else "Medium"
    elif abs(margin) < 5:
        preferred_side = "NONE"
        risk = "High"
    else:
        preferred_side = "NONE"
        risk = "Medium"

    # Market closed → no live edge to read; be explicit about it rather
    # than showing a stale intraday bias.
    if not spot_data.get("market_open", True):
        market_bias = "Sideways"
        preferred_side = "NONE"

    # Review #3/#4 hard gate: market_regime.py's NO_TRADE covers cases
    # run_decision_engine didn't otherwise know about — the first 30 min
    # after open (false-signal-prone), VIX > 30 (extreme/unpredictable), or
    # market_open=False caught a different way than the spot-flag check
    # above. A "hard gate" per the review means this ACTUALLY blocks a
    # directional call, not just a lower confidence number next to one.
    #
    # Deliberately checking `regime == "NO_TRADE"` here, NOT
    # `regime_info.get("no_trade")` — that boolean is also True for
    # HIGH_VOLATILITY (market_regime.py sets it that way to gate its own
    # OPTION BUYING/SELLING strategy suggestion, a different concern from
    # "should this engine even venture a direction"). HIGH_VOLATILITY
    # already gets its own, less absolute treatment via
    # REGIME_CONFIDENCE_MULTIPLIERS below (confidence cut, not a hard
    # block) — matching the review's own example of "confidence reduced",
    # not "NO TRADE", for high VIX specifically.
    if regime == "NO_TRADE":
        market_bias = "Sideways"
        preferred_side = "NONE"
        risk = "High"

    # ── Confidence (rule-based, not random) ──────────────────────────────
    # Maximum possible = the dampened theoretical max (see
    # _max_dampened_score / MAX_DAMPENED_SCORE above), not a raw weight sum —
    # since bull/bear are now bucket-dampened (review #7), normalising
    # against the old undampened sum would make confidence read
    # artificially low across the board.
    max_possible = MAX_DAMPENED_SCORE
    dominant = max(bull, bear)
    raw_conf = (dominant / max_possible) * 100 if max_possible > 0 else 0

    # Agreement factor: if bull+bear strongly disagree, lower confidence
    disagreement = min(bull, bear) / max(max(bull, bear), 1)
    confidence = int(raw_conf * (1 - disagreement * 0.4))

    # ── Data completeness dampening (review: neutral-because-no-data must
    # cost confidence, not just silently contribute 0) ────────────────────
    availability = _data_availability(market_data, market_data.get("technical_data_source", ""))
    total_weight = sum(WEIGHTS.values()) or 1
    unavailable_weight = sum(w for name, w in WEIGHTS.items() if not availability.get(name, True))
    data_completeness_pct = round((1 - unavailable_weight / total_weight) * 100)
    # Missing HALF the possible weight → up to ~25% confidence cut; missing
    # everything → up to 50% cut. Deliberately not a full wipeout even at
    # 0% completeness — the few indicators that DID score still say
    # something real, just with a smaller, honestly-labelled sample.
    if unavailable_weight > 0:
        confidence = int(confidence * (1 - (unavailable_weight / total_weight) * 0.5))

    # ── Volatility context (VIX + ATR, non-directional) ──────────────────
    vix = market_data.get("vix", 15)
    atr_val = technicals.get("atr", 0)
    atr_pct = (atr_val / spot * 100) if spot > 0 else 0.0
    vol_ctx = _volatility_context(vix, atr_pct)

    # High volatility + no clear trend (weak ADX) = genuinely less certain
    # about direction, even if the bull/bear tally looks lopsided — a wide,
    # choppy range can flip a "70% bullish" read within minutes. Dampen
    # confidence in that specific case instead of pretending certainty.
    adx_for_conf = technicals.get("adx", 0)
    if vol_ctx.get("regime") == "high" and adx_for_conf < 20:
        confidence = int(confidence * 0.8)

    # Review #4: regime-specific confidence cut (e.g. HIGH_VOLATILITY,
    # EXPIRY_HIGH_GAMMA) — distinct from and stacks with the VIX/ATR check
    # above, since it also covers regimes that check doesn't (gamma risk
    # near expiry has nothing to do with ADX).
    regime_conf_mult = REGIME_CONFIDENCE_MULTIPLIERS.get(regime)
    if regime_conf_mult is not None:
        confidence = int(confidence * regime_conf_mult)

    confidence = max(30, min(95, confidence))  # clamp 30-95

    # ── Market Forecast ───────────────────────────────────────────────────
    if vol_ctx.get("regime") == "high":
        forecast = "High Volatility"
    elif vol_ctx.get("regime") == "low":
        forecast = "Low Volatility"
    elif margin >= 15:
        forecast = "Bullish"
    elif margin <= -15:
        forecast = "Bearish"
    elif abs(margin) < 8:
        forecast = "Sideways"
    else:
        forecast = "Neutral"

    # ── Option Strategy (Sideways-ஐ விட actionable info தர) ──────────────
    adx_val = technicals.get("adx", 0)
    strategy, strategy_reason = _suggest_strategy(
        preferred_side, margin, adx_val, vix, atr_pct
    )

    # ── Scenarios + invalidation levels ───────────────────────────────────
    # "What would prove this view wrong" is as important as the view itself
    # — a bias without an invalidation level isn't falsifiable. Built from
    # the same support/resistance levels already computed for this request.
    sr = market_data.get("support_resistance", {}) or {}
    scenarios = _build_scenarios(spot, sr, bullish_probability, bearish_probability, margin)

    # ── Recommended strike (ATM by default; ATM+1 when direction bias is
    # weak/uncertain — cheaper premium for a less-confident read) ────────
    if preferred_side == "NONE":
        recommended_strike = "NONE"
    elif adx_val >= 25 and abs(margin) >= 20:
        recommended_strike = "ATM"
    else:
        recommended_strike = "ATM+1"

    return {
        "market_bias":           market_bias,
        "bullish_probability":   bullish_probability,
        "bearish_probability":   bearish_probability,
        "preferred_side":        preferred_side,
        "recommended_strike":    recommended_strike,
        "bull_score":            bull,
        "bear_score":            bear,
        # Pre-dampening sums, kept only for debugging/transparency (review
        # #7) — NOT used for market_bias/confidence/preferred_side, which
        # all derive from the dampened bull/bear above.
        "raw_bull_score":        scores["bull"],
        "raw_bear_score":        scores["bear"],
        "neutral_score":         scores["neutral"],
        "confidence":            confidence,
        # Same number as `confidence` — surfaced under a name that doesn't
        # imply "X% probability this move happens" (a real statistical
        # claim this rule engine doesn't make). UI should show this as
        # "Signal Strength: N/100", not "N% confident".
        "signal_strength":       confidence,
        # Review: how much of the 20-condition weight actually had live
        # data behind it — confidence above is already dampened by this,
        # but surfacing the raw % lets the UI explain WHY (e.g. "62% —
        # Angel One futures/Gift Nifty/FII unavailable right now").
        "data_completeness_pct": data_completeness_pct,
        "risk":                  risk,
        "forecast":              forecast,
        "volatility_regime":     vol_ctx.get("regime", "unknown"),
        "volatility_label":      vol_ctx.get("label", ""),
        # Review #4: market_regime.py's classification, now actually
        # feeding the scoring above (bucket reweighting + confidence cut +
        # NO_TRADE hard gate) instead of only being computed for the
        # separate /strategy route. Surfaced here so the UI can show WHY
        # trend indicators mattered more/less today.
        "market_regime":         regime,
        "market_regime_confidence": regime_info.get("confidence", "LOW"),
        "market_regime_reasons": regime_info.get("reasons", []),
        "market_regime_no_trade": regime_info.get("no_trade", False),
        "market_regime_no_trade_reason": regime_info.get("no_trade_reason", ""),
        "margin":                margin,
        "reasons":               reasons,
        "strategy":              strategy,
        "strategy_reason":       strategy_reason,
        "scenarios":             scenarios,
    }
