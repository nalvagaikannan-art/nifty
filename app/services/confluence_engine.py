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
    spot = market_data.get("spot", {}).get("price", 0)
    vwap = market_data.get("technicals", {}).get("vwap", 0)
    w = FACTOR_WEIGHTS["vwap"]
    if vwap <= 0:
        return _factor("vwap", "neutral", 0, "VWAP unavailable")
    diff = (spot - vwap) / vwap * 100
    if diff > 0.3:
        return _factor("vwap", "bullish", w if diff > 0.5 else w * 0.6,
                        f"Spot {spot:.0f} above VWAP {vwap:.0f} (+{diff:.2f}%)")
    if diff < -0.3:
        return _factor("vwap", "bearish", w if diff < -0.5 else w * 0.6,
                        f"Spot {spot:.0f} below VWAP {vwap:.0f} ({diff:.2f}%)")
    return _factor("vwap", "neutral", 0, f"Spot near VWAP {vwap:.0f}")


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
    rsi = market_data.get("rsi", 50)
    w   = FACTOR_WEIGHTS["rsi"]
    if rsi >= 60:
        return _factor("rsi", "bullish", w, f"RSI {rsi:.1f} — momentum bullish")
    if rsi >= 55:
        return _factor("rsi", "bullish", w * 0.5, f"RSI {rsi:.1f} — mild bullish")
    if rsi <= 40:
        return _factor("rsi", "bearish", w, f"RSI {rsi:.1f} — momentum bearish")
    if rsi <= 45:
        return _factor("rsi", "bearish", w * 0.5, f"RSI {rsi:.1f} — mild bearish")
    return _factor("rsi", "neutral", 0, f"RSI {rsi:.1f} — neutral zone")


def _eval_macd(market_data: Dict) -> Dict:
    macd = market_data.get("macd", {})
    w    = FACTOR_WEIGHTS["macd"]
    if not macd:
        return _factor("macd", "neutral", 0, "MACD unavailable")
    line = macd.get("macd", 0) or 0
    sig  = macd.get("signal", 0) or 0
    hist = macd.get("histogram", line - sig)
    if line > sig and hist > 0:
        return _factor("macd", "bullish", w, f"MACD {line:.1f} > Signal {sig:.1f}, histogram positive")
    if line > sig:
        return _factor("macd", "bullish", w * 0.5, f"MACD above signal, histogram weak")
    if line < sig and hist < 0:
        return _factor("macd", "bearish", w, f"MACD {line:.1f} < Signal {sig:.1f}, histogram negative")
    if line < sig:
        return _factor("macd", "bearish", w * 0.5, f"MACD below signal, histogram weak")
    return _factor("macd", "neutral", 0, "MACD near signal")


def _eval_adx(market_data: Dict) -> Dict:
    tech   = market_data.get("technicals", {})
    adx    = tech.get("adx", 0)
    di_pos = tech.get("di_plus", 0)
    di_neg = tech.get("di_minus", 0)
    w      = FACTOR_WEIGHTS["adx"]
    if adx <= 0:
        return _factor("adx", "neutral", 0, "ADX unavailable")
    if adx < 18:
        return _factor("adx", "neutral", 0, f"ADX {adx:.1f} < 18 — no clear trend")
    if di_pos > di_neg:
        score = w if adx >= 25 else w * 0.6
        return _factor("adx", "bullish", score, f"ADX {adx:.1f}, +DI {di_pos:.1f} > -DI {di_neg:.1f} — bullish trend")
    if di_neg > di_pos:
        score = w if adx >= 25 else w * 0.6
        return _factor("adx", "bearish", score, f"ADX {adx:.1f}, -DI {di_neg:.1f} > +DI {di_pos:.1f} — bearish trend")
    return _factor("adx", "neutral", 0, f"ADX {adx:.1f} — DI equal")


def _eval_supertrend(market_data: Dict) -> Dict:
    st = market_data.get("technicals", {}).get("supertrend", "")
    w  = FACTOR_WEIGHTS["supertrend"]
    if not st:
        return _factor("supertrend", "neutral", 0, "Supertrend unavailable")
    st_lower = str(st).lower()
    if "bull" in st_lower or "up" in st_lower:
        return _factor("supertrend", "bullish", w, f"Supertrend: {st} — bullish")
    if "bear" in st_lower or "down" in st_lower:
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
    tech = market_data.get("technicals", {})
    spike = tech.get("volume_spike", False)
    ratio = tech.get("volume_ratio", 1.0)
    dec   = market_data.get("decision", {})
    bias  = dec.get("market_bias", "Sideways")
    w     = FACTOR_WEIGHTS["volume"]
    if not spike:
        return _factor("volume", "neutral", 0, f"Volume ratio {ratio:.1f}x — normal")
    if "Bull" in bias:
        return _factor("volume", "bullish", w,
                        f"Volume spike {ratio:.1f}x with bullish bias — confirms move")
    if "Bear" in bias:
        return _factor("volume", "bearish", w,
                        f"Volume spike {ratio:.1f}x with bearish bias — confirms move")
    return _factor("volume", "neutral", w * 0.3,
                    f"Volume spike {ratio:.1f}x — direction unclear")


def _eval_pcr(market_data: Dict) -> Dict:
    pcr = market_data.get("pcr", 0)
    w   = FACTOR_WEIGHTS["pcr"]
    if pcr <= 0:
        return _factor("pcr", "neutral", 0, "PCR unavailable")
    if pcr >= 1.3:
        return _factor("pcr", "bullish", w, f"PCR {pcr:.2f} — strong put writing, bullish")
    if pcr >= 1.1:
        return _factor("pcr", "bullish", w * 0.6, f"PCR {pcr:.2f} — mild put writing")
    if pcr <= 0.7:
        return _factor("pcr", "bearish", w, f"PCR {pcr:.2f} — strong call writing, bearish")
    if pcr <= 0.9:
        return _factor("pcr", "bearish", w * 0.6, f"PCR {pcr:.2f} — mild call writing")
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
    oi_sum = market_data.get("oi_summary", {})
    spot   = market_data.get("spot", {}).get("price", 0)
    ce_top = oi_sum.get("ce_max_oi_strike", 0)
    w      = FACTOR_WEIGHTS["call_writing"]
    if not ce_top or not spot:
        return _factor("call_writing", "neutral", 0, "Call writing data unavailable")
    dist = (ce_top - spot) / spot * 100
    if 0 < dist < 2.0:
        return _factor("call_writing", "bearish", w,
                        f"Heavy call writing at {ce_top:.0f} — resistance {dist:.1f}% above spot")
    if dist > 3.0:
        return _factor("call_writing", "bullish", w * 0.4,
                        f"Call wall far at {ce_top:.0f} — ceiling far, room to move up")
    return _factor("call_writing", "neutral", 0,
                    f"Call writing at {ce_top:.0f} — neutral zone")


def _eval_put_writing(market_data: Dict) -> Dict:
    oi_sum = market_data.get("oi_summary", {})
    spot   = market_data.get("spot", {}).get("price", 0)
    pe_top = oi_sum.get("pe_max_oi_strike", 0)
    w      = FACTOR_WEIGHTS["put_writing"]
    if not pe_top or not spot:
        return _factor("put_writing", "neutral", 0, "Put writing data unavailable")
    dist = (spot - pe_top) / spot * 100
    if 0 < dist < 2.0:
        return _factor("put_writing", "bullish", w,
                        f"Heavy put writing at {pe_top:.0f} — support {dist:.1f}% below spot")
    if dist > 3.0:
        return _factor("put_writing", "bearish", w * 0.4,
                        f"Put wall far at {pe_top:.0f} — floor weak")
    return _factor("put_writing", "neutral", 0,
                    f"Put writing at {pe_top:.0f} — neutral zone")


def _eval_india_vix(market_data: Dict) -> Dict:
    vix = market_data.get("vix", 15)
    w   = FACTOR_WEIGHTS["india_vix"]
    if vix <= 0:
        return _factor("india_vix", "neutral", 0, "VIX unavailable")
    # VIX is non-directional — but extreme low = complacency (bull risk)
    # extreme high = fear spike (potential reversal)
    if vix < 11:
        return _factor("india_vix", "neutral", 0,
                        f"VIX {vix:.1f} — extreme complacency, reversal risk")
    if vix > 25:
        return _factor("india_vix", "neutral", 0,
                        f"VIX {vix:.1f} — HIGH fear, directional signal unreliable")
    if vix <= 15:
        return _factor("india_vix", "bullish", w * 0.5,
                        f"VIX {vix:.1f} — low, stable environment")
    return _factor("india_vix", "neutral", 0, f"VIX {vix:.1f} — moderate")


def _eval_global_market(market_data: Dict) -> Dict:
    chg    = market_data.get("global_change_pct", 0)
    status = market_data.get("global_status", "live")
    w      = FACTOR_WEIGHTS["global_market"]
    if status != "live":
        return _factor("global_market", "neutral", 0, "Global market data unavailable")
    if chg > 0.5:
        return _factor("global_market", "bullish", w, f"Global markets +{chg:.1f}% — risk-on")
    if chg > 0.2:
        return _factor("global_market", "bullish", w * 0.5, f"Global markets +{chg:.1f}% — mild positive")
    if chg < -0.5:
        return _factor("global_market", "bearish", w, f"Global markets {chg:.1f}% — risk-off")
    if chg < -0.2:
        return _factor("global_market", "bearish", w * 0.5, f"Global markets {chg:.1f}% — mild negative")
    return _factor("global_market", "neutral", 0, f"Global markets {chg:.2f}% — flat")


def _eval_fii_dii(market_data: Dict) -> Dict:
    fii    = market_data.get("fii_net_cr", 0)
    dii    = market_data.get("dii_net_cr", 0)
    fii_st = market_data.get("fii_status", "live")
    dii_st = market_data.get("dii_status", "live")
    w      = FACTOR_WEIGHTS["fii_dii"]
    net    = 0
    if fii_st == "live":
        net += fii
    if dii_st == "live":
        net += dii
    if fii_st != "live" and dii_st != "live":
        return _factor("fii_dii", "neutral", 0, "FII/DII data unavailable")
    if net > 500:
        return _factor("fii_dii", "bullish", w, f"FII+DII net ₹{net:.0f}Cr — strong buying")
    if net > 100:
        return _factor("fii_dii", "bullish", w * 0.5, f"FII+DII net ₹{net:.0f}Cr — mild buying")
    if net < -500:
        return _factor("fii_dii", "bearish", w, f"FII+DII net ₹{net:.0f}Cr — strong selling")
    if net < -100:
        return _factor("fii_dii", "bearish", w * 0.5, f"FII+DII net ₹{net:.0f}Cr — mild selling")
    return _factor("fii_dii", "neutral", 0, f"FII+DII net ₹{net:.0f}Cr — neutral")


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
    if margin > 10:
        direction = "BULLISH"
    elif margin < -10:
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
