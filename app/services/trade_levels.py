"""
Trade Levels Engine — V2
=========================
Entry / SL / T1 / T2 / T3 — structure + ATR அடிப்படையில் dynamic ஆக calculate செய்கிறோம்.
Hard-coded percentages இல்லை.

Signal → Setup zone → Confirmation → Entry

SL: NIFTY structure (support/resistance)
Targets: 1R, 2R, trailing

Output per trade setup:
    trigger_level   - நீங்கள் இந்த level break ஆனால் entry
    entry_zone      - Entry price range
    stop_loss       - Structure-based SL
    risk_per_lot    - SL பெரிசு
    target_1        - 1R
    target_2        - 2R
    target_3        - 2.5R (trailing)
    rr_ratio        - R:R
    setup_quality   - HIGH / MEDIUM / LOW
    reasons         - [str]
"""

from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# ── Minimum acceptable R:R ────────────────────────────────────────────────
MIN_RR = 1.5
PREFERRED_RR = 2.0


def _nearest_support(spot: float, supports: List[float]) -> Optional[float]:
    """Spot-க்கு கீழே உள்ள closest support"""
    below = [s for s in supports if s < spot]
    return max(below) if below else None


def _nearest_resistance(spot: float, resistances: List[float]) -> Optional[float]:
    """Spot-க்கு மேலே உள்ள closest resistance"""
    above = [r for r in resistances if r > spot]
    return min(above) if above else None


def _atr_buffer(atr: float, multiplier: float = 0.5) -> float:
    """ATR-based buffer for SL placement"""
    return round(atr * multiplier, 1)


def calculate_trade_levels(
    market_data: Dict,
    direction: str,       # "bullish" or "bearish"
    spot: float,
    option_ltp: float,    # Current option premium
    lot_size: int = 50,   # NIFTY lot size
    delta: Optional[float] = None,          # real Black-Scholes delta for the chosen strike, if known
    theta_per_day: Optional[float] = None,  # ₹/day decay for the chosen strike, if known (negative)
    assumed_hold_days: float = 1.0,         # how long you expect to hold before hitting T2 — used to net out theta
) -> Dict:
    """
    Structure + ATR அடிப்படையில் trade levels calculate செய்கிறோம்.

    Args:
        market_data: Full market data
        direction: "bullish" (BUY CE) or "bearish" (BUY PE)
        spot: Current NIFTY spot price
        option_ltp: Option premium
        lot_size: Lot size (default 50 for NIFTY)
        delta: real per-strike delta (options_greeks.black_scholes_greeks) —
            when given, this REPLACES the old fixed 0.45 approximation used
            to map underlying-point risk to option-premium risk. Review
            point #38: "Option premium SL/Target — underlying ATR மட்டும்
            போதாது" — a deep-OTM strike (delta ~0.20) and a deep-ITM strike
            (delta ~0.80) do NOT move the same ₹ amount for the same spot
            move, so using one fixed constant for every strike was wrong.
        theta_per_day: real per-strike theta — when given, expected decay
            over `assumed_hold_days` is netted OUT of the targets and OUT of
            the SL cushion, so the levels reflect "what the premium can
            realistically do net of time decay", not just the directional
            move in isolation.
    """
    tech   = market_data.get("technicals", {}) or {}
    sr     = market_data.get("support_resistance", {}) or {}
    atr    = tech.get("atr", 0)
    vwap   = tech.get("vwap", 0)

    supports    = [s for s in (sr.get("support", []) or []) if s > 0]
    resistances = [r for r in (sr.get("resistance", []) or []) if r > 0]

    atr_pct = (atr / spot * 100) if spot > 0 and atr > 0 else 0
    reasons: List[str] = []

    # Guard: invalid spot
    if spot <= 0:
        return {
            "direction": direction, "trigger": 0, "entry_zone": (0, 0), "entry_mid": 0,
            "stop_loss_spot": 0, "risk_spot": 0, "target_1_spot": 0, "target_2_spot": 0,
            "target_3_spot": 0, "option_entry": None, "option_sl": None,
            "option_t1": None, "option_t2": None, "option_t3": None,
            "risk_per_lot": 0, "rr_ratio": 0, "setup_quality": "LOW",
            "atr": atr, "atr_pct": 0, "reasons": ["Spot price unavailable"], "lot_size": lot_size,
        }

    # ── 1. Trigger level (confirmation needed before entry) ───────────────
    if direction == "bullish":
        # Break above recent resistance or VWAP
        near_res = _nearest_resistance(spot, resistances)
        if near_res and (near_res - spot) / spot < 0.01:
            trigger = near_res
            reasons.append(f"Trigger: NIFTY > {trigger:.0f} (break above resistance)")
        elif vwap > spot:
            trigger = vwap
            reasons.append(f"Trigger: NIFTY > {trigger:.0f} (reclaim VWAP)")
        else:
            trigger = spot + _atr_buffer(atr, 0.3)
            reasons.append(f"Trigger: NIFTY > {trigger:.0f} (ATR-based breakout)")

        # Entry zone: just above trigger
        entry_low  = trigger
        entry_high = trigger + _atr_buffer(atr, 0.2)

        # SL: nearest support below spot
        near_sup = _nearest_support(spot, supports)
        if near_sup and (spot - near_sup) / spot < 0.025:
            sl_spot  = near_sup - _atr_buffer(atr, 0.3)   # buffer below support
            sl_method = f"structure SL at {near_sup:.0f} support − buffer"
        elif vwap > 0:
            sl_spot  = vwap - _atr_buffer(atr, 0.5)
            sl_method = f"VWAP {vwap:.0f} − ATR buffer"
        else:
            sl_spot  = spot - atr * 1.5
            sl_method = f"1.5× ATR below entry ({atr:.0f})"

        reasons.append(f"SL basis: {sl_method}")

    else:  # bearish
        # Break below support or VWAP
        near_sup = _nearest_support(spot, supports)
        if near_sup and (spot - near_sup) / spot < 0.01:
            trigger = near_sup
            reasons.append(f"Trigger: NIFTY < {trigger:.0f} (break below support)")
        elif vwap > 0 and vwap < spot:
            trigger = vwap
            reasons.append(f"Trigger: NIFTY < {trigger:.0f} (lose VWAP)")
        else:
            trigger = spot - _atr_buffer(atr, 0.3)
            reasons.append(f"Trigger: NIFTY < {trigger:.0f} (ATR-based breakdown)")

        entry_low  = trigger - _atr_buffer(atr, 0.2)
        entry_high = trigger

        # SL: nearest resistance above spot
        near_res = _nearest_resistance(spot, resistances)
        if near_res and (near_res - spot) / spot < 0.025:
            sl_spot  = near_res + _atr_buffer(atr, 0.3)
            sl_method = f"structure SL at {near_res:.0f} resistance + buffer"
        elif vwap > 0:
            sl_spot  = vwap + _atr_buffer(atr, 0.5)
            sl_method = f"VWAP {vwap:.0f} + ATR buffer"
        else:
            sl_spot  = spot + atr * 1.5
            sl_method = f"1.5× ATR above entry ({atr:.0f})"

        reasons.append(f"SL basis: {sl_method}")

    # ── 2. Risk (underlying) ──────────────────────────────────────────────
    if direction == "bullish":
        risk_spot = abs(entry_low - sl_spot)
    else:
        risk_spot = abs(sl_spot - entry_high)

    if risk_spot <= 0:
        risk_spot = atr * 1.0   # fallback

    # ── 3. Targets (1R, 2R, 2.5R) on underlying ──────────────────────────
    if direction == "bullish":
        t1_spot = entry_low + risk_spot * 1.0
        t2_spot = entry_low + risk_spot * 2.0
        t3_spot = entry_low + risk_spot * 2.5
    else:
        t1_spot = entry_high - risk_spot * 1.0
        t2_spot = entry_high - risk_spot * 2.0
        t3_spot = entry_high - risk_spot * 2.5

    rr_ratio = round(risk_spot * 2.0 / risk_spot, 1)  # T2 = 2R always

    # ── 4. Option premium levels ────────────────────────────────────────────
    # Fallback constant only used when a real per-strike delta isn't
    # available — near-ATM options move roughly ₹0.45 per ₹1 of spot, but
    # this is now a LAST RESORT, not the default (see docstring).
    DELTA_APPROX_FALLBACK = 0.45
    eff_delta = abs(delta) if delta is not None else DELTA_APPROX_FALLBACK
    delta_is_real = delta is not None

    if option_ltp > 0:
        opt_risk = round(risk_spot * eff_delta, 1)
        opt_sl   = round(option_ltp - opt_risk, 1)
        opt_t1   = round(option_ltp + opt_risk * 1.0, 1)
        opt_t2   = round(option_ltp + opt_risk * 2.0, 1)
        opt_t3   = round(option_ltp + opt_risk * 2.5, 1)

        # Net theta decay OUT of the targets (review #23/#38: a
        # directionally-correct trade can still lose money to time decay —
        # a target that ignores this overstates what's realistically
        # reachable). SL gets a little decay cushion too (needs a slightly
        # bigger adverse move to trigger, since decay is already working
        # against the position independent of direction).
        theta_note = ""
        if theta_per_day is not None and theta_per_day < 0 and assumed_hold_days > 0:
            expected_decay = round(abs(theta_per_day) * assumed_hold_days, 1)
            opt_t1 = round(opt_t1 - expected_decay, 1)
            opt_t2 = round(opt_t2 - expected_decay, 1)
            opt_t3 = round(opt_t3 - expected_decay, 1)
            opt_sl = round(opt_sl - expected_decay * 0.5, 1)
            theta_note = f", net of ~₹{expected_decay:.1f} expected theta decay over {assumed_hold_days:.0f}d"

        # Premium SL floor: never let option go to zero
        premium_sl_floor = round(option_ltp * 0.40, 1)
        opt_sl = max(opt_sl, premium_sl_floor)

        delta_basis = f"real delta {eff_delta:.2f}" if delta_is_real else f"approx delta {eff_delta:.2f} (real Greeks unavailable)"
        reasons.append(
            f"Option SL ₹{opt_sl:.1f} — max(structure-based using {delta_basis}, 40% premium erosion){theta_note}"
        )
        if not delta_is_real:
            reasons.append("⚠️ Using fallback delta approximation — pass the strike's real Greeks for accurate premium levels")
    else:
        opt_risk = opt_sl = opt_t1 = opt_t2 = opt_t3 = 0
        reasons.append("Option premium data unavailable — spot-based SL only")

    # ── 5. Risk per lot ───────────────────────────────────────────────────
    risk_per_lot = round(opt_risk * lot_size, 0) if opt_risk > 0 else round(risk_spot * DELTA_APPROX * lot_size, 0)

    # ── 6. Setup quality ──────────────────────────────────────────────────
    has_structure_sl = any(s in reasons[1] for s in ["structure", "VWAP", "support", "resistance"])
    if rr_ratio >= PREFERRED_RR and has_structure_sl and atr_pct > 0:
        quality = "HIGH"
    elif rr_ratio >= MIN_RR:
        quality = "MEDIUM"
    else:
        quality = "LOW"
        reasons.append(f"⚠️ R:R {rr_ratio:.1f} below minimum {MIN_RR} — consider skipping")

    return {
        "direction":       direction,
        "trigger":         round(trigger, 1),
        "entry_zone":      (round(entry_low, 1), round(entry_high, 1)),
        "entry_mid":       round((entry_low + entry_high) / 2, 1),
        "stop_loss_spot":  round(sl_spot, 1),
        "risk_spot":       round(risk_spot, 1),
        "target_1_spot":   round(t1_spot, 1),
        "target_2_spot":   round(t2_spot, 1),
        "target_3_spot":   round(t3_spot, 1),
        # Option premium levels
        "option_entry":    round(option_ltp, 1) if option_ltp > 0 else None,
        "option_sl":       opt_sl if option_ltp > 0 else None,
        "option_t1":       opt_t1 if option_ltp > 0 else None,
        "option_t2":       opt_t2 if option_ltp > 0 else None,
        "option_t3":       opt_t3 if option_ltp > 0 else None,
        "delta_used":      round(eff_delta, 2) if option_ltp > 0 else None,
        "delta_is_real":   delta_is_real if option_ltp > 0 else None,
        "theta_per_day_used": theta_per_day,
        "risk_per_lot":    risk_per_lot,
        "rr_ratio":        rr_ratio,
        "setup_quality":   quality,
        "atr":             round(atr, 1),
        "atr_pct":         round(atr_pct, 2),
        "reasons":         reasons,
        "lot_size":        lot_size,
    }
