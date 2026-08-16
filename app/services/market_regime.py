"""
Market Regime Engine — V2
==========================
Current market conditions-ஐ classify செய்கிறோம்.

Regimes:
    TREND_UP, TREND_DOWN, RANGE,
    HIGH_VOLATILITY, LOW_VOLATILITY,
    BREAKOUT, BREAKDOWN,
    EXPIRY_HIGH_GAMMA, NO_TRADE

Output:
    regime: str
    confidence: HIGH / MEDIUM / LOW
    option_buying_allowed: bool
    option_selling_allowed: bool
    preferred_strategy: str
    reasons: [str]
    no_trade: bool
    no_trade_reason: str
"""

from typing import Dict, List, Tuple
from datetime import datetime, time
from zoneinfo import ZoneInfo
import logging

try:
    from app.utils.helpers import expiry_filter as _expiry_filter
except ImportError:
    def _expiry_filter(s):
        return {"days_left": 7, "warning": None}

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# ── Regime definitions ────────────────────────────────────────────────────
REGIME_TREND_UP       = "TREND_UP"
REGIME_TREND_DOWN     = "TREND_DOWN"
REGIME_RANGE          = "RANGE"
REGIME_HIGH_VOL       = "HIGH_VOLATILITY"
REGIME_LOW_VOL        = "LOW_VOLATILITY"
REGIME_BREAKOUT       = "BREAKOUT"
REGIME_BREAKDOWN      = "BREAKDOWN"
REGIME_EXPIRY_GAMMA   = "EXPIRY_HIGH_GAMMA"
REGIME_NO_TRADE       = "NO_TRADE"


def _is_expiry_week(days_to_expiry: int) -> bool:
    return days_to_expiry <= 2


def _time_session() -> str:
    # BUG FIX (2026-08-16): datetime.now() used server-local time (UTC on
    # Render). IST = UTC+5:30 so 9:15 IST = 3:45 UTC — all session
    # boundaries were off by 5h30m. Fixed: datetime.now(_IST).
    now = datetime.now(_IST).time()
    open_  = time(9, 15)
    open30 = time(9, 45)
    close_ = time(15, 30)
    close_early = time(15, 0)
    if now < open_:
        return "PRE_MARKET"
    if now < open30:
        return "OPENING"
    if now < close_early:
        return "MID"
    if now <= close_:
        return "CLOSING"
    return "POST_MARKET"


def classify_market_regime(market_data: Dict, confluence: Dict = None) -> Dict:
    """
    Market regime classify செய்கிறோம்.

    Args:
        market_data: Full market_data dict from MarketAnalyzer
        confluence:  Optional output from run_confluence_engine()

    Returns:
        Regime dict with trading implications
    """
    tech         = market_data.get("technicals", {}) or {}
    spot_data    = market_data.get("spot", {}) or {}
    spot         = spot_data.get("price", 0)
    prev_close   = spot_data.get("prev_close", 0)
    market_open  = spot_data.get("market_open", True)
    vix          = market_data.get("vix", 15)
    adx          = tech.get("adx", 0)
    atr          = tech.get("atr", 0)
    ema20        = tech.get("ema20", 0)
    ema50        = tech.get("ema50", 0)
    supertrend   = str(tech.get("supertrend", "")).lower()
    di_plus      = tech.get("di_plus", 0)
    di_minus     = tech.get("di_minus", 0)

    # OI / chain data
    oi_sum       = market_data.get("oi_summary", {}) or {}
    atm_iv       = oi_sum.get("atm_iv", 0)

    # Expiry
    chain_data   = market_data.get("option_chain", {}) or {}
    expiry_str   = chain_data.get("expiry", "")
    expiry_info  = _expiry_filter(expiry_str)
    days_to_exp  = expiry_info.get("days_left", 7)

    # Derived metrics
    atr_pct  = (atr / spot * 100) if spot > 0 and atr > 0 else 0
    gap_pct  = ((spot - prev_close) / prev_close * 100) if prev_close > 0 and spot > 0 else 0
    session  = _time_session()

    reasons: List[str] = []
    no_trade = False
    no_trade_reason = ""

    # ── Priority 1: Market closed (explicit flag) ─────────────────────────
    if not market_open:
        return {
            "regime":                REGIME_NO_TRADE,
            "confidence":            "HIGH",
            "option_buying_allowed": False,
            "option_selling_allowed": False,
            "preferred_strategy":    "WAIT",
            "reasons":               ["Market is closed"],
            "no_trade":              True,
            "no_trade_reason":       "Market closed",
            "session":               session,
            "days_to_expiry":        days_to_exp,
            "atr_pct":               atr_pct,
            "gap_pct":               gap_pct,
            "vix":                   vix,
            "adx":                   adx,
        }

    # ── Priority 1b: Pre/Post market (warn only — market_open is the authority) ──
    if session in ("PRE_MARKET", "POST_MARKET"):
        reasons.append(f"Session: {session} — market may be closed")

    # ── Priority 2: Opening volatility (9:15–9:45) ───────────────────────
    if session == "OPENING":
        no_trade = True
        no_trade_reason = "Opening 30 min — false signals likely, wait for range to form"
        reasons.append(no_trade_reason)

    # ── Priority 3: Extreme VIX ───────────────────────────────────────────
    if vix > 30:
        no_trade = True
        no_trade_reason = f"VIX {vix:.1f} > 30 — extreme fear, unpredictable moves"
        reasons.append(f"⚠️ {no_trade_reason}")

    # ── Priority 4: Expiry high gamma ────────────────────────────────────
    if _is_expiry_week(days_to_exp):
        reasons.append(f"Expiry in {days_to_exp} day(s) — high gamma, pin risk")

    # ── Gap detection ─────────────────────────────────────────────────────
    if abs(gap_pct) > 1.5:
        if gap_pct > 0:
            reasons.append(f"Gap up +{gap_pct:.1f}% — breakout possible")
        else:
            reasons.append(f"Gap down {gap_pct:.1f}% — breakdown possible")

    # ── Regime classification logic ───────────────────────────────────────
    if no_trade:
        regime = REGIME_NO_TRADE
        confidence = "HIGH"

    elif vix > 25:
        regime = REGIME_HIGH_VOL
        confidence = "HIGH"
        reasons.append(f"VIX {vix:.1f} > 25 — high volatility regime")

    elif gap_pct > 1.5 and adx >= 20:
        regime = REGIME_BREAKOUT
        confidence = "HIGH" if adx >= 25 else "MEDIUM"
        reasons.append(f"Gap up +{gap_pct:.1f}% with ADX {adx:.1f} — breakout")

    elif gap_pct < -1.5 and adx >= 20:
        regime = REGIME_BREAKDOWN
        confidence = "HIGH" if adx >= 25 else "MEDIUM"
        reasons.append(f"Gap down {gap_pct:.1f}% with ADX {adx:.1f} — breakdown")

    elif adx >= 25 and di_plus > di_minus:
        # Strong uptrend
        ema_confirm = (spot > ema20 > ema50) if ema20 > 0 and ema50 > 0 else True
        st_confirm  = "bull" in supertrend or "up" in supertrend
        confidence  = "HIGH" if (ema_confirm and st_confirm) else "MEDIUM"
        regime = REGIME_TREND_UP
        reasons.append(f"ADX {adx:.1f}, +DI {di_plus:.1f} > -DI {di_minus:.1f} — strong uptrend")
        if ema_confirm:
            reasons.append(f"EMA20 {ema20:.0f} > EMA50 {ema50:.0f} — trend confirmed")

    elif adx >= 25 and di_minus > di_plus:
        # Strong downtrend
        ema_confirm = (spot < ema20 < ema50) if ema20 > 0 and ema50 > 0 else True
        st_confirm  = "bear" in supertrend or "down" in supertrend
        confidence  = "HIGH" if (ema_confirm and st_confirm) else "MEDIUM"
        regime = REGIME_TREND_DOWN
        reasons.append(f"ADX {adx:.1f}, -DI {di_minus:.1f} > +DI {di_plus:.1f} — strong downtrend")
        if ema_confirm:
            reasons.append(f"EMA20 {ema20:.0f} < EMA50 {ema50:.0f} — downtrend confirmed")

    elif adx < 18:
        regime = REGIME_RANGE
        confidence = "HIGH" if adx < 15 else "MEDIUM"
        reasons.append(f"ADX {adx:.1f} < 18 — range bound, no trend")

    elif _is_expiry_week(days_to_exp):
        # MINOR BUG FIX (2026-08-16): EXPIRY_HIGH_GAMMA was checked AFTER
        # LOW_VOLATILITY, so expiry week + VIX<12 + ATR<0.5 gave LOW_VOL
        # regime instead of EXPIRY_GAMMA. Expiry week is the more specific
        # and action-relevant condition — check it first.
        regime = REGIME_EXPIRY_GAMMA
        confidence = "HIGH"
        reasons.append(f"Expiry in {days_to_exp} day(s) — gamma risk dominant")

    elif vix < 12 and atr_pct < 0.5:
        regime = REGIME_LOW_VOL
        confidence = "MEDIUM"
        reasons.append(f"VIX {vix:.1f} low + ATR {atr_pct:.1f}% — compressed, breakout possible")

    else:
        regime = REGIME_RANGE
        confidence = "MEDIUM"
        reasons.append(f"ADX {adx:.1f} — no clear trend, treat as range")

    # ── Strategy implications ─────────────────────────────────────────────
    buying_regimes  = {REGIME_TREND_UP, REGIME_TREND_DOWN, REGIME_BREAKOUT, REGIME_BREAKDOWN}
    selling_regimes = {REGIME_RANGE, REGIME_LOW_VOL}
    no_trade_regimes = {REGIME_NO_TRADE, REGIME_HIGH_VOL}

    if regime in no_trade_regimes:
        option_buying_allowed  = False
        option_selling_allowed = False
        preferred_strategy     = "NO TRADE"
    elif regime in buying_regimes:
        option_buying_allowed  = True
        option_selling_allowed = False
        preferred_strategy     = "DIRECTIONAL BUYING"
    elif regime == REGIME_EXPIRY_GAMMA:
        option_buying_allowed  = False   # gamma too dangerous for buyers near expiry
        option_selling_allowed = True
        preferred_strategy     = "THETA SELLING (tight strikes)"
    elif regime in selling_regimes:
        option_buying_allowed  = False
        option_selling_allowed = True
        preferred_strategy     = "THETA SELLING / IRON CONDOR"
    else:
        option_buying_allowed  = False
        option_selling_allowed = False
        preferred_strategy     = "WAIT"

    # Confluence adjustment: low quality confluence → reduce buying confidence
    if confluence and confluence.get("quality") == "LOW" and option_buying_allowed:
        reasons.append("⚠️ Confluence quality LOW — reduce size or wait")

    return {
        "regime":                 regime,
        "confidence":             confidence,
        "option_buying_allowed":  option_buying_allowed,
        "option_selling_allowed": option_selling_allowed,
        "preferred_strategy":     preferred_strategy,
        "reasons":                reasons,
        "no_trade":               no_trade or regime in no_trade_regimes,
        "no_trade_reason":        no_trade_reason,
        "session":                session,
        "days_to_expiry":         days_to_exp,
        "atr_pct":                round(atr_pct, 2),
        "gap_pct":                round(gap_pct, 2),
        "vix":                    vix,
        "adx":                    adx,
    }
