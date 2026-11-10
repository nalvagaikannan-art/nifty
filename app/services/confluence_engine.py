"""
Confluence Engine — V2
=======================
18 factors-ஐ ஒரே இடத்தில் evaluate செய்கிறோம்.
ஒவ்வொரு factor-க்கும் structured result return செய்கிறோம்.
Double-counting இல்லை.

Output:
    bull_score, bear_score, neutral_score
    confluence_score (0-100 normalized)
    agreement_count / total_factors
    direction: BULLISH / BEARISH / NEUTRAL
    quality: HIGH / MEDIUM / LOW
    factors: [{id, direction, score, max_score, reason}]
"""

from typing import Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)

# ── Max scores per factor (sums to 100 for normalization) ─────────────────
FACTOR_WEIGHTS = {
    "trend":          8,
    "vwap":           7,
    "ema20":          5,
    "ema50":          5,
    "rsi":            5,
    "macd":           5,
    "adx":            4,
    "supertrend":     5,
    "atr":            3,   # non-directional — dampening only
    "volume":         5,
    "pcr":            8,
    "oi_change":      7,
    "call_writing":   5,
    "put_writing":    5,
    "india_vix":      5,
    "global_market":  5,
    "fii_dii":        6,
    "price_structure": 7,
}

TOTAL_WEIGHT = sum(FACTOR_WEIGHTS.values())  # 100


def _factor(
    fid: str,
    direction: str,   # "bullish", "bearish", "neutral"
    score: float,     # 0 to max_score
    reason: str,
) -> Dict:
    return {
        "id":        fid,
        "direction": direction,
        "score":     round(score, 1),
        "max_score": FACTOR_WEIGHTS[fid],
        "reason":    reason,
    }


# ── Individual factor evaluators ──────────────────────────────────────────

def _eval_trend(market_data: Dict) -> Dict:
    """Spot vs prev_close — basic trend"""
    spot = market_data.get("spot", {}).get("price", 0)
    prev = market_data.get("spot", {}).get("prev_close", 0)
    if spot <= 0 or prev <= 0:
        return _factor("trend", "neutral", 0, "Price data unavailable")
    chg = (spot - prev) / prev * 100
    w = FACTOR_WEIGHTS["trend"]
    if chg > 0.8:
        return _factor("trend", "bullish", w, f"Spot +{chg:.1f}% above prev close — uptrend")
    if chg > 0.3:
        return _factor("trend", "bullish", w * 0.5, f"Spot +{chg:.1f}% mild uptrend")
    if chg < -0.8:
        return _factor("trend", "bearish", w, f"Spot {chg:.1f}% below prev close — downtrend")
    if chg < -0.3:
        return _factor("trend", "bearish", w * 0.5, f"Spot {chg:.1f}% mild downtrend")
    return _factor("trend", "neutral", 0, f"Spot {chg:.2f}% — flat")


def _eval_vwap(market_data: Dict) -> Dict:
    """Use decision_engine VWAP direction threshold exactly.

    Directional VWAP evidence requires:
      spot > VWAP by more than 0.3% -> bullish
      spot < VWAP by more than 0.3% -> bearish
      otherwise -> neutral

    Confluence keeps its own factor weight (7), but does not introduce
    a separate 0.5% half/full threshold that Decision Engine does not use.
    """
    spot = market_data.get("spot", {}).get("price", 0)
    vwap = market_data.get("technicals", {}).get("vwap", 0)
    w = FACTOR_WEIGHTS["vwap"]

    if spot <= 0 or vwap <= 0:
        return _factor("vwap", "neutral", 0, "VWAP unavailable")

    diff = (spot - vwap) / vwap * 100

    if diff > 0.3:
        return _factor(
            "vwap", "bullish", w,
            f"Spot {spot:.0f} above VWAP {vwap:.0f} (+{diff:.2f}%)"
        )

    if diff < -0.3:
        return _factor(
            "vwap", "bearish", w,
            f"Spot {spot:.0f} below VWAP {vwap:.0f} ({diff:.2f}%)"
        )

    return _factor(
        "vwap", "neutral", 0,
        f"Spot near VWAP {vwap:.0f}"
    )
def _eval_ema20(market_data: Dict) -> Dict:
    spot = market_data.get("spot", {}).get("price", 0)
    ema  = market_data.get("technicals", {}).get("ema20", 0)
    w    = FACTOR_WEIGHTS["ema20"]
    if ema <= 0:
        return _factor("ema20", "neutral", 0, "EMA20 unavailable")
    diff = (spot - ema) / ema * 100
    if diff > 0.2:
        return _factor("ema20", "bullish", w, f"Above EMA20 {ema:.0f} (+{diff:.1f}%)")
    if diff < -0.2:
        return _factor("ema20", "bearish", w, f"Below EMA20 {ema:.0f} ({diff:.1f}%)")
    return _factor("ema20", "neutral", 0, f"Near EMA20 {ema:.0f}")


def _eval_ema50(market_data: Dict) -> Dict:
    spot = market_data.get("spot", {}).get("price", 0)
    ema  = market_data.get("technicals", {}).get("ema50", 0)
    w    = FACTOR_WEIGHTS["ema50"]
    if ema <= 0:
        return _factor("ema50", "neutral", 0, "EMA50 unavailable")
    diff = (spot - ema) / ema * 100
    if diff > 0.2:
        return _factor("ema50", "bullish", w, f"Above EMA50 {ema:.0f} (+{diff:.1f}%)")
    if diff < -0.2:
        return _factor("ema50", "bearish", w, f"Below EMA50 {ema:.0f} ({diff:.1f}%)")
    return _factor("ema50", "neutral", 0, f"Near EMA50 {ema:.0f}")


def _eval_rsi(market_data: Dict) -> Dict:
    """Use decision_engine RSI semantics and scoring exactly.

    RSI >= 70 is overbought/caution (bearish),
    60-69.9 is bullish momentum,
    <= 30 is oversold/bounce potential (bullish),
    30-40 is bearish momentum,
    otherwise neutral.
    """
    rsi = market_data.get("rsi", 50)
    w = FACTOR_WEIGHTS["rsi"]

    if rsi >= 70:
        return _factor(
            "rsi", "bearish", w,
            f"RSI {rsi:.1f} — overbought, caution"
        )

    if rsi >= 60:
        return _factor(
            "rsi", "bullish", int(w * 0.7),
            f"RSI {rsi:.1f} — bullish momentum"
        )

    if rsi <= 30:
        return _factor(
            "rsi", "bullish", w,
            f"RSI {rsi:.1f} — oversold, bounce possible"
        )

    if rsi <= 40:
        return _factor(
            "rsi", "bearish", int(w * 0.7),
            f"RSI {rsi:.1f} — bearish momentum"
        )

    return _factor(
        "rsi", "neutral", 0,
        f"RSI {rsi:.1f} — neutral"
    )
def _eval_macd(market_data: Dict) -> Dict:
    """Use strict MACD confirmation semantics from decision_engine.

    A directional MACD signal requires both:
      line > signal AND histogram > 0  -> bullish
      line < signal AND histogram < 0  -> bearish
    A line-only cross is neutral rather than a half-strength direction.
    """
    macd = market_data.get("macd", {})
    w = FACTOR_WEIGHTS["macd"]

    if not macd:
        return _factor("macd", "neutral", 0, "MACD unavailable")

    line = macd.get("macd", 0) or 0
    sig = macd.get("signal", 0) or 0
    hist = macd.get("histogram", line - sig)

    if line > sig and hist > 0:
        return _factor(
            "macd", "bullish", w,
            f"MACD {line:.1f} > Signal {sig:.1f}, histogram positive"
        )

    if line < sig and hist < 0:
        return _factor(
            "macd", "bearish", w,
            f"MACD {line:.1f} < Signal {sig:.1f}, histogram negative"
        )

    return _factor(
        "macd", "neutral", 0,
        f"MACD {line:.1f} — no clear crossover"
    )
def _eval_adx(market_data: Dict) -> Dict:
    """Use decision_engine ADX semantics exactly.

    ADX measures trend strength; direction comes from +DI versus -DI.
    Below 20 is treated as no reliable trend. At/above 20, the directional
    signal receives the full ADX weight.
    """
    tech = market_data.get("technicals", {})
    adx = tech.get("adx", 0)
    di_pos = tech.get("di_plus", 0)
    di_neg = tech.get("di_minus", 0)
    w = FACTOR_WEIGHTS["adx"]

    if adx <= 0:
        return _factor("adx", "neutral", 0, "ADX unavailable")

    if adx < 20:
        return _factor(
            "adx", "neutral", 0,
            f"ADX {adx:.1f} < 20 — no clear trend"
        )

    if di_pos > di_neg:
        return _factor(
            "adx", "bullish", w,
            f"ADX {adx:.1f}, +DI {di_pos:.1f} > -DI {di_neg:.1f} — strong uptrend"
        )

    if di_neg > di_pos:
        return _factor(
            "adx", "bearish", w,
            f"ADX {adx:.1f}, -DI {di_neg:.1f} > +DI {di_pos:.1f} — strong downtrend"
        )

    return _factor(
        "adx", "neutral", 0,
        f"ADX {adx:.1f} — DI equal"
    )
def _eval_supertrend(market_data: Dict) -> Dict:
    st = market_data.get("technicals", {}).get("supertrend", "")
    w  = FACTOR_WEIGHTS["supertrend"]
    if not st:
        return _factor("supertrend", "neutral", 0, "Supertrend unavailable")
    st_lower = str(st).lower()
    if "bull" in st_lower or "up" in st_lower or st_lower in ("buy", "long"):
        return _factor("supertrend", "bullish", w, f"Supertrend: {st} — bullish")
    if "bear" in st_lower or "down" in st_lower or st_lower in ("sell", "short"):
        return _factor("supertrend", "bearish", w, f"Supertrend: {st} — bearish")
    return _factor("supertrend", "neutral", 0, f"Supertrend: {st} — unclear")


def _eval_atr(market_data: Dict) -> Dict:
    """ATR is non-directional — returns as neutral with volatility context"""
    tech = market_data.get("technicals", {})
    atr  = tech.get("atr", 0)
    spot = market_data.get("spot", {}).get("price", 0)
    w    = FACTOR_WEIGHTS["atr"]
    if atr <= 0 or spot <= 0:
        return _factor("atr", "neutral", 0, "ATR unavailable")
    atr_pct = atr / spot * 100
    if atr_pct > 1.5:
        return _factor("atr", "neutral", 0,
                        f"ATR {atr:.0f} ({atr_pct:.1f}%) — HIGH volatility, both directions possible")
    return _factor("atr", "neutral", 0,
                    f"ATR {atr:.0f} ({atr_pct:.1f}%) — normal range")


def _eval_volume(market_data: Dict) -> Dict:
    """Volume spike gets direction from spot vs VWAP, not market_bias.

    Volume itself has no direction:
      spot > VWAP -> bullish attribution
      spot < VWAP -> bearish attribution
      VWAP unavailable -> neutral
    """
    tech = market_data.get("technicals", {})
    spot = market_data.get("spot", {}).get("price", 0)
    vwap = tech.get("vwap", 0)
    spike = tech.get("volume_spike", False)
    ratio = tech.get("volume_ratio", 1.0)
    w = FACTOR_WEIGHTS["volume"]

    if not spike:
        return _factor("volume", "neutral", 0,
                       f"Volume ratio {ratio:.1f}x — normal")

    if spot <= 0 or vwap <= 0:
        return _factor("volume", "neutral", 0,
                       f"Volume spike {ratio:.1f}x — direction unavailable (VWAP missing)")

    if spot > vwap:
        if ratio > 1.5:
            return _factor("volume", "bullish", w,
                           f"Volume spike {ratio:.1f}x, spot above VWAP — buying interest")
        return _factor("volume", "bullish", w * 0.5,
                       f"Volume elevated {ratio:.1f}x, spot above VWAP — mild buying interest")

    if spot < vwap:
        if ratio > 1.5:
            return _factor("volume", "bearish", w,
                           f"Volume spike {ratio:.1f}x, spot below VWAP — selling interest")
        return _factor("volume", "bearish", w * 0.5,
                       f"Volume elevated {ratio:.1f}x, spot below VWAP — mild selling interest")

    return _factor("volume", "neutral", 0,
                   f"Volume spike {ratio:.1f}x, spot at VWAP — direction unclear")
def _eval_pcr(market_data: Dict) -> Dict:
    pcr = market_data.get("pcr", 0)
    w   = FACTOR_WEIGHTS["pcr"]
    if pcr <= 0:
        return _factor("pcr", "neutral", 0, "PCR unavailable")
    if pcr >= 1.3:
        return _factor("pcr", "bullish", w, f"PCR {pcr:.2f} — strong put writing, bullish")
    if pcr >= 1.1:
        return _factor("pcr", "bullish", w * 0.5, f"PCR {pcr:.2f} — mild put writing")
    if pcr <= 0.7:
        return _factor("pcr", "bearish", w, f"PCR {pcr:.2f} — strong call writing, bearish")
    if pcr <= 0.9:
        return _factor("pcr", "bearish", w * 0.5, f"PCR {pcr:.2f} — mild call writing")
    return _factor("pcr", "neutral", 0, f"PCR {pcr:.2f} — neutral")


def _eval_oi_change(market_data: Dict) -> Dict:
    oi  = market_data.get("oi_change", {})
    ce  = oi.get("ce_change", 0)
    pe  = oi.get("pe_change", 0)
    w   = FACTOR_WEIGHTS["oi_change"]
    if pe > ce and pe > 0:
        return _factor("oi_change", "bullish", w, f"PE OI buildup (+{pe:,}) > CE — bullish pressure")
    if ce > pe and ce > 0:
        return _factor("oi_change", "bearish", w, f"CE OI buildup (+{ce:,}) > PE — bearish pressure")
    return _factor("oi_change", "neutral", 0, "OI change balanced")


def _eval_call_writing(market_data: Dict) -> Dict:
    """Classify call positioning using OI/LTP change when available.

    Max OI by itself is not enough to call something "writing".
    When change data is unavailable, use proximity only at reduced weight.
    This mirrors decision_engine._score_call_writing().
    """
    oi_sum = market_data.get("oi_summary", {})
    spot = market_data.get("spot", {}).get("price", 0)
    ce_top = oi_sum.get("ce_max_oi_strike", 0)
    ce_oi_chg = oi_sum.get("ce_oi_chg_atm", None)
    ce_price_chg = oi_sum.get("ce_ltp_chg_atm", None)
    w = FACTOR_WEIGHTS["call_writing"]

    if not ce_top or not spot:
        return _factor("call_writing", "neutral", 0,
                       "Call writing data unavailable")

    dist = ce_top - spot

    if ce_oi_chg is not None and ce_price_chg is not None:
        oi_building = ce_oi_chg > 0
        price_falling = ce_price_chg < 0
        price_rising = ce_price_chg > 0

        if oi_building and price_falling and 0 < dist < spot * 0.025:
            return _factor(
                "call_writing", "bearish", w,
                f"Call writing confirmed at {ce_top:.0f} — OI↑ Price↓ — overhead resistance"
            )

        if oi_building and price_rising:
            return _factor(
                "call_writing", "bullish", w * 0.6,
                f"Call buying at {ce_top:.0f} — OI↑ Price↑ — bullish demand"
            )

        if not oi_building:
            return _factor(
                "call_writing", "bullish", w * 0.4,
                f"Call unwinding at {ce_top:.0f} — OI↓ — bearish pressure easing"
            )

        if 0 < dist < spot * 0.02:
            return _factor(
                "call_writing", "bearish", int(w * 0.5),
                f"Call OI buildup near {ce_top:.0f} — price direction unclear"
            )

        return _factor(
            "call_writing", "neutral", 0,
            f"Call OI at {ce_top:.0f} — direction unclear"
        )

    if 0 < dist < spot * 0.02:
        return _factor(
            "call_writing", "bearish", int(w * 0.5),
            f"Max Call OI near {ce_top:.0f} — possible resistance (OI change unavailable)"
        )

    if dist > spot * 0.03:
        return _factor(
            "call_writing", "bullish", int(w * 0.2),
            f"Max Call OI far OTM at {ce_top:.0f} — less resistance"
        )

    return _factor("call_writing", "neutral", 0,
                   "Call OI pattern neutral")
def _eval_put_writing(market_data: Dict) -> Dict:
    """Classify put positioning using OI/LTP change when available.

    Max OI by itself is not enough to call something "writing".
    When change data is unavailable, use proximity only at reduced weight.
    This mirrors decision_engine._score_put_writing().
    """
    oi_sum = market_data.get("oi_summary", {})
    spot = market_data.get("spot", {}).get("price", 0)
    pe_top = oi_sum.get("pe_max_oi_strike", 0)
    pe_oi_chg = oi_sum.get("pe_oi_chg_atm", None)
    pe_price_chg = oi_sum.get("pe_ltp_chg_atm", None)
    w = FACTOR_WEIGHTS["put_writing"]

    if not pe_top or not spot:
        return _factor("put_writing", "neutral", 0,
                       "Put writing data unavailable")

    dist = spot - pe_top

    if pe_oi_chg is not None and pe_price_chg is not None:
        oi_building = pe_oi_chg > 0
        price_falling = pe_price_chg < 0
        price_rising = pe_price_chg > 0

        if oi_building and price_falling and 0 < dist < spot * 0.025:
            return _factor(
                "put_writing", "bullish", w,
                f"Put writing confirmed at {pe_top:.0f} — OI↑ Price↓ — strong support"
            )

        if oi_building and price_rising:
            return _factor(
                "put_writing", "bearish", w * 0.6,
                f"Put buying at {pe_top:.0f} — OI↑ Price↑ — bearish demand"
            )

        if not oi_building:
            return _factor(
                "put_writing", "bearish", w * 0.4,
                f"Put unwinding at {pe_top:.0f} — OI↓ — bullish pressure easing"
            )

        if 0 < dist < spot * 0.02:
            return _factor(
                "put_writing", "bullish", int(w * 0.5),
                f"Put OI buildup near {pe_top:.0f} — price direction unclear"
            )

        return _factor(
            "put_writing", "neutral", 0,
            f"Put OI at {pe_top:.0f} — direction unclear"
        )

    if 0 < dist < spot * 0.02:
        return _factor(
            "put_writing", "bullish", int(w * 0.5),
            f"Max Put OI near {pe_top:.0f} — possible support (OI change unavailable)"
        )

    if dist > spot * 0.03:
        return _factor(
            "put_writing", "bearish", int(w * 0.2),
            f"Max Put OI far OTM at {pe_top:.0f} — weak support"
        )

    return _factor("put_writing", "neutral", 0,
                   "Put OI pattern neutral")
def _eval_india_vix(market_data: Dict) -> Dict:
    """VIX is volatility context, not directional evidence."""
    vix = market_data.get("vix", 15)
    if vix <= 0:
        return _factor("india_vix", "neutral", 0, "VIX unavailable")
    if vix < 11:
        return _factor(
            "india_vix", "neutral", 0,
            f"VIX {vix:.1f} — extreme complacency, reversal risk"
        )
    if vix > 25:
        return _factor(
            "india_vix", "neutral", 0,
            f"VIX {vix:.1f} — high fear, directional signal unreliable"
        )
    if vix <= 15:
        return _factor(
            "india_vix", "neutral", 0,
            f"VIX {vix:.1f} — low, stable volatility environment"
        )
    return _factor(
        "india_vix", "neutral", 0,
        f"VIX {vix:.1f} — moderate volatility"
    )
def _eval_global_market(market_data: Dict) -> Dict:
    """Use the same global-market thresholds as decision_engine.

    +0.5% / -0.5% are directional thresholds; the middle band is neutral.
    """
    chg = market_data.get("global_change_pct", 0)
    status = market_data.get("global_status", "live")
    w = FACTOR_WEIGHTS["global_market"]

    if status != "live":
        return _factor(
            "global_market", "neutral", 0,
            "Global market data unavailable"
        )

    if chg > 0.5:
        return _factor(
            "global_market", "bullish", w,
            f"Global markets +{chg:.1f}% — positive"
        )

    if chg < -0.5:
        return _factor(
            "global_market", "bearish", w,
            f"Global markets {chg:.1f}% — negative"
        )

    return _factor(
        "global_market", "neutral", 0,
        f"Global markets {chg:.1f}% — flat"
    )
def _eval_fii_dii(market_data: Dict) -> Dict:
    """Combine FII and DII using decision_engine directional semantics.

    Each flow contributes up to half of this factor's total weight (3/6).
    FII mild negative values (-500..0) are bearish, matching
    decision_engine._score_fii(). DII mild negative values (-500..0)
    remain neutral, matching decision_engine._score_dii().
    """
    fii = market_data.get("fii_net_cr", 0)
    dii = market_data.get("dii_net_cr", 0)
    fii_st = market_data.get("fii_status", "live")
    dii_st = market_data.get("dii_status", "live")
    w = FACTOR_WEIGHTS["fii_dii"]
    half = w / 2.0

    def flow_score(value, status, mild_negative=True):
        if status != "live":
            return 0.0

        if value > 500:
            return half
        if value > 0:
            return half * 0.5
        if value < -500:
            return -half
        if value < 0 and mild_negative:
            return -half * 0.5
        return 0.0

    if fii_st != "live" and dii_st != "live":
        return _factor(
            "fii_dii", "neutral", 0,
            "FII/DII data unavailable"
        )

    fii_component = flow_score(fii, fii_st, mild_negative=True)

    # Match decision_engine._score_dii():
    # mild negatives (-500..0) are neutral for DII.
    dii_component = flow_score(dii, dii_st, mild_negative=False)

    combined = fii_component + dii_component

    if combined > 0:
        return _factor(
            "fii_dii", "bullish", min(w, combined),
            f"FII ₹{fii:.0f}Cr / DII ₹{dii:.0f}Cr — combined flow supports bullish side"
        )

    if combined < 0:
        return _factor(
            "fii_dii", "bearish", min(w, abs(combined)),
            f"FII ₹{fii:.0f}Cr / DII ₹{dii:.0f}Cr — combined flow supports bearish side"
        )

    return _factor(
        "fii_dii", "neutral", 0,
        f"FII ₹{fii:.0f}Cr / DII ₹{dii:.0f}Cr — flows balanced"
    )


def _eval_price_structure(market_data: Dict) -> Dict:

    """Support/resistance structure vs current spot"""
    spot = market_data.get("spot", {}).get("price", 0)
    sr   = market_data.get("support_resistance", {}) or {}
    supports    = sr.get("support", [])
    resistances = sr.get("resistance", [])
    w = FACTOR_WEIGHTS["price_structure"]

    if not spot or (not supports and not resistances):
        return _factor("price_structure", "neutral", 0, "S/R data unavailable")

    near_support    = any(abs(s - spot) / spot < 0.01 for s in supports)
    near_resistance = any(abs(r - spot) / spot < 0.01 for r in resistances)
    above_resistance = resistances and spot > min(resistances)
    below_support    = supports and spot < max(supports)

    if above_resistance:
        return _factor("price_structure", "bullish", w,
                        f"Spot {spot:.0f} broke above resistance — bullish structure")
    if near_support and not near_resistance:
        return _factor("price_structure", "bullish", w * 0.6,
                        f"Spot near support — potential bounce")
    if below_support:
        return _factor("price_structure", "bearish", w,
                        f"Spot {spot:.0f} broke below support — bearish structure")
    if near_resistance and not near_support:
        return _factor("price_structure", "bearish", w * 0.6,
                        f"Spot near resistance — potential rejection")
    return _factor("price_structure", "neutral", 0, "Spot in no-man's land between S/R")


# ── Main Confluence Engine ────────────────────────────────────────────────

def run_confluence_engine(market_data: Dict) -> Dict:
    """
    18 factors evaluate செய்து confluence score return செய்கிறோம்.
    Each factor is evaluated independently — no double-counting.
    """
    evaluators = [
        _eval_trend,
        _eval_vwap,
        _eval_ema20,
        _eval_ema50,
        _eval_rsi,
        _eval_macd,
        _eval_adx,
        _eval_supertrend,
        _eval_atr,
        _eval_volume,
        _eval_pcr,
        _eval_oi_change,
        _eval_call_writing,
        _eval_put_writing,
        _eval_india_vix,
        _eval_global_market,
        _eval_fii_dii,
        _eval_price_structure,
    ]

    factors: List[Dict] = []
    bull_score = 0.0
    bear_score = 0.0
    neutral_score = 0.0
    agreement_bull = 0
    agreement_bear = 0
    total_factors  = 0

    for fn in evaluators:
        try:
            f = fn(market_data)
        except Exception as exc:
            logger.warning(f"Confluence factor {fn.__name__} error: {exc}")
            continue

        factors.append(f)
        total_factors += 1
        d = f["direction"]
        s = f["score"]

        if d == "bullish":
            bull_score   += s
            agreement_bull += 1
        elif d == "bearish":
            bear_score   += s
            agreement_bear += 1
        else:
            neutral_score += f["max_score"] - s  # unscored weight → neutral

    # Normalize to 0-100
    max_possible = TOTAL_WEIGHT
    dominant_score = max(bull_score, bear_score)
    confluence_score = round(dominant_score / max_possible * 100) if max_possible > 0 else 0
    confluence_score = max(0, min(100, confluence_score))

    # Direction
    margin = bull_score - bear_score
    if margin >= 10:
        direction = "BULLISH"
    elif margin <= -10:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    # Agreement count (factors clearly pointing same way)
    if direction == "BULLISH":
        agreement_count = agreement_bull
    elif direction == "BEARISH":
        agreement_count = agreement_bear
    else:
        agreement_count = 0

    # Quality
    if confluence_score >= 65 and agreement_count >= 6:
        quality = "HIGH"
    elif confluence_score >= 45 and agreement_count >= 4:
        quality = "MEDIUM"
    else:
        quality = "LOW"

    return {
        "bull_score":       round(bull_score, 1),
        "bear_score":       round(bear_score, 1),
        "neutral_score":    round(neutral_score, 1),
        "confluence_score": confluence_score,
        "agreement_count":  agreement_count,
        "total_factors":    total_factors,
        "direction":        direction,
        "quality":          quality,
        "margin":           round(margin, 1),
        "factors":          factors,
    }
