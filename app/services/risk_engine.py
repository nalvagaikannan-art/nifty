"""
Risk Engine — V2
================
Existing risk_manager.py-ஐ replace/extend செய்கிறோம்.
Position sizing, daily loss limit, NO-TRADE conditions.

Inputs:
    capital, max_risk_pct, entry, stop_loss,
    lot_size, open_positions, daily_pnl,
    market_regime, vix, days_to_expiry, signal_strength

Output:
    allowed: bool
    risk_level: LOW / MEDIUM / HIGH / EXTREME
    risk_amount: ₹
    quantity: lots
    max_loss: ₹
    risk_reward: float
    reasons: [str]
    no_trade_reasons: [str]

IMPORTANT: Signal strength அதிகம் என்பதற்காக position size அதிகரிக்கல.
"""

from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

# ── Default risk parameters ────────────────────────────────────────────────
DEFAULT_MAX_RISK_PCT      = 1.0    # Capital-ல் 1% max per trade
DEFAULT_DAILY_MAX_LOSS_PCT = 3.0   # Capital-ல் 3% max daily loss
MAX_OPEN_POSITIONS        = 3      # Concurrent positions limit
MIN_RR_RATIO              = 1.5    # Minimum R:R to enter
MIN_OPTION_LTP            = 5.0    # Below this → no liquidity
MIN_OI_FOR_TRADE          = 500    # Minimum OI
NIFTY_LOT_SIZE            = 50

# ── NO-TRADE conditions ────────────────────────────────────────────────────
NO_TRADE_CONDITIONS = {
    "daily_loss_exceeded":   "Daily max loss limit reached",
    "too_many_positions":    "Too many open positions",
    "invalid_sl":            "Stop-loss cannot be defined or invalid",
    "poor_liquidity":        "Option liquidity too poor",
    "extreme_vix":           "VIX extreme — unpredictable",
    "no_regime_clarity":     "Market regime unclear",
    "weak_confluence":       "Confluence quality too low",
    "low_rr":                "R:R below minimum threshold",
    "signal_flipping":       "Signal rapidly flipping — wait for stability",
    "expiry_gamma":          "Expiry high-gamma — directional buying risky",
    "spread_too_wide":       "Bid-ask spread too wide",
    "zero_option_price":     "Option LTP is zero — no price discovery",
    "stale_data":            "Market data stale or unavailable",
    "major_event_window":    "Major event in window — avoid",
}


def assess_risk(
    capital: float,
    entry_price: float,       # Option LTP
    stop_loss_price: float,   # Option SL level
    rr_ratio: float,          # From trade_levels
    market_regime: str,
    vix: float,
    days_to_expiry: int,
    confluence_quality: str,
    signal_strength: int,
    open_positions: int = 0,
    daily_pnl: float = 0.0,
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
    daily_max_loss_pct: float = DEFAULT_DAILY_MAX_LOSS_PCT,
    oi: float = 0,
    volume: float = 0,
    lot_size: int = NIFTY_LOT_SIZE,
) -> Dict:
    """
    Full risk assessment + position sizing.
    """
    no_trade_reasons: List[str] = []
    warnings: List[str] = []
    reasons: List[str] = []

    # ── 1. Check all NO-TRADE conditions first ────────────────────────────

    # Daily loss exceeded
    daily_loss_limit = capital * (daily_max_loss_pct / 100)
    if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit:
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['daily_loss_exceeded']} "
            f"(Loss ₹{abs(daily_pnl):.0f} ≥ limit ₹{daily_loss_limit:.0f})"
        )

    # Too many open positions
    if open_positions >= MAX_OPEN_POSITIONS:
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['too_many_positions']} ({open_positions}/{MAX_OPEN_POSITIONS})"
        )

    # Invalid SL
    if stop_loss_price <= 0 or entry_price <= 0:
        no_trade_reasons.append(NO_TRADE_CONDITIONS["invalid_sl"])
    elif stop_loss_price >= entry_price:
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['invalid_sl']} — SL ₹{stop_loss_price} ≥ entry ₹{entry_price}"
        )

    # Option zero price
    if entry_price <= 0:
        no_trade_reasons.append(NO_TRADE_CONDITIONS["zero_option_price"])

    # Poor liquidity
    if oi > 0 and oi < MIN_OI_FOR_TRADE:
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['poor_liquidity']} (OI {oi:.0f} < {MIN_OI_FOR_TRADE})"
        )
    if volume == 0 and oi > 0:
        no_trade_reasons.append(f"{NO_TRADE_CONDITIONS['poor_liquidity']} — zero volume today")
    if 0 < entry_price < MIN_OPTION_LTP:
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['poor_liquidity']} — LTP ₹{entry_price:.1f} too low"
        )

    # Extreme VIX
    if vix > 30:
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['extreme_vix']} (VIX {vix:.1f} > 30)"
        )

    # Regime: NO_TRADE / HIGH_VOLATILITY
    if market_regime in ("NO_TRADE", "HIGH_VOLATILITY"):
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['no_regime_clarity']} — regime: {market_regime}"
        )

    # Expiry gamma risk for buyers
    if market_regime == "EXPIRY_HIGH_GAMMA" or days_to_expiry <= 1:
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['expiry_gamma']} (expiry in {days_to_expiry} day(s))"
        )

    # Weak confluence
    if confluence_quality == "LOW":
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['weak_confluence']} — quality: LOW"
        )

    # Low R:R
    if rr_ratio > 0 and rr_ratio < MIN_RR_RATIO:
        no_trade_reasons.append(
            f"{NO_TRADE_CONDITIONS['low_rr']} (R:R {rr_ratio:.1f} < {MIN_RR_RATIO})"
        )

    # If any NO-TRADE condition triggered, return immediately
    if no_trade_reasons:
        return {
            "allowed":          False,
            "risk_level":       "EXTREME",
            "risk_amount":      0,
            "quantity":         0,
            "lots":             0,
            "max_loss":         0,
            "risk_reward":      rr_ratio,
            "reasons":          reasons,
            "no_trade_reasons": no_trade_reasons,
            "warnings":         warnings,
        }

    # ── 2. Position sizing ────────────────────────────────────────────────
    # NOTE: Signal strength does NOT increase position size.
    # Fixed fraction of capital only.

    max_risk_amount = capital * (max_risk_pct / 100)
    risk_per_lot = (entry_price - stop_loss_price) * lot_size

    if risk_per_lot <= 0:
        no_trade_reasons.append("Risk per lot calculation failed")
        return {
            "allowed":          False,
            "risk_level":       "EXTREME",
            "risk_amount":      0,
            "quantity":         0,
            "lots":             0,
            "max_loss":         0,
            "risk_reward":      rr_ratio,
            "reasons":          reasons,
            "no_trade_reasons": no_trade_reasons,
            "warnings":         warnings,
        }

    lots = int(max_risk_amount / risk_per_lot)
    lots = max(1, min(lots, 5))   # Floor: 1 lot, ceiling: 5 lots (safety cap)

    quantity    = lots * lot_size
    max_loss    = round(risk_per_lot * lots, 0)
    risk_amount = max_loss

    reasons.append(f"Capital ₹{capital:,.0f} × {max_risk_pct:.1f}% = ₹{max_risk_amount:.0f} max risk")
    reasons.append(f"Risk per lot: ₹{risk_per_lot:.1f} × {lots} lots = ₹{max_loss:.0f}")

    # ── 3. Risk level classification ──────────────────────────────────────
    if vix > 22 or market_regime in ("HIGH_VOLATILITY", "EXPIRY_HIGH_GAMMA"):
        risk_level = "HIGH"
        warnings.append(f"High VIX {vix:.1f} — elevated risk")
    elif confluence_quality == "MEDIUM" or signal_strength < 55:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    if days_to_expiry <= 3:
        risk_level = "HIGH"
        warnings.append(f"Expiry in {days_to_expiry} days — theta decay accelerates")

    # ── 4. Summary ────────────────────────────────────────────────────────
    return {
        "allowed":           True,
        "risk_level":        risk_level,
        "risk_amount":       risk_amount,
        "quantity":          quantity,
        "lots":              lots,
        "max_loss":          max_loss,
        "risk_reward":       rr_ratio,
        "risk_per_lot":      round(risk_per_lot, 1),
        "max_risk_amount":   round(max_risk_amount, 0),
        "reasons":           reasons,
        "no_trade_reasons":  [],
        "warnings":          warnings,
    }


# ── Backward-compatible wrapper for old risk_manager usage ────────────────
class RiskManager:
    """Legacy wrapper — old code compatibility"""

    @staticmethod
    def assess_risk(market_data: Dict) -> Dict:
        vix    = market_data.get("vix", 0)
        pcr    = market_data.get("pcr", 0)
        trend  = market_data.get("trend", "sideways")
        score  = 0
        if vix > 25:
            score += 30
        elif vix > 20:
            score += 20
        if pcr < 0.7 or pcr > 1.3:
            score += 20
        if trend == "bearish":
            score += 20
        elif trend == "bullish":
            score += 10
        level = "low" if score < 30 else "medium" if score < 60 else "high"
        return {"score": score, "level": level}
