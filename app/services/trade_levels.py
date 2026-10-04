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
from app.utils.helpers import intraday_hold_days_to_close

from app.services.contract_specs import resolve_lot_size

logger = logging.getLogger(__name__)

# ── Minimum acceptable R:R ────────────────────────────────────────────────
MIN_RR = 1.5
PREFERRED_RR = 2.0

# ── Regime-adaptive target/SL profile ──────────────────────────────────────
# BUG FIX (2026-08-22): targets were ALWAYS 1R / 2R / 2.5R and SL fallback
# was ALWAYS 1.5×ATR, regardless of market regime. In a RANGE / LOW_VOLATILITY
# session (low ADX, tiny ATR — exactly what a "no clear trend" day looks
# like) price rarely travels a full 2R after triggering, so T2/T3 were
# effectively unreachable — trades sat open with neither target nor SL hit.
# In HIGH_VOLATILITY / expiry-gamma sessions the opposite problem shows up:
# a 1.5×ATR SL is too tight and gets stopped out by normal noise before the
# real move happens. This profile scales BOTH the target R-multiples and the
# ATR-based SL buffer/fallback to the regime so levels match what that
# regime can realistically deliver.
#   t1_r, t2_r, t3_r      -> target R-multiples (t2 defines the quoted RR)
#   sl_atr_mult           -> ATR multiplier for the "no structure" SL fallback
#   trigger_buf_mult      -> ATR multiplier for trigger/entry-zone buffers
#   sl_struct_buf_mult    -> ATR multiplier for the buffer added past support/resistance
REGIME_LEVEL_PROFILES: Dict[str, Dict[str, float]] = {
    "TREND_UP":            {"t1_r": 1.0, "t2_r": 2.0, "t3_r": 2.5, "sl_atr_mult": 1.5, "trigger_buf_mult": 0.3, "sl_struct_buf_mult": 0.3},
    "TREND_DOWN":          {"t1_r": 1.0, "t2_r": 2.0, "t3_r": 2.5, "sl_atr_mult": 1.5, "trigger_buf_mult": 0.3, "sl_struct_buf_mult": 0.3},
    "BREAKOUT":            {"t1_r": 1.0, "t2_r": 2.0, "t3_r": 3.0, "sl_atr_mult": 1.5, "trigger_buf_mult": 0.3, "sl_struct_buf_mult": 0.3},
    "BREAKDOWN":           {"t1_r": 1.0, "t2_r": 2.0, "t3_r": 3.0, "sl_atr_mult": 1.5, "trigger_buf_mult": 0.3, "sl_struct_buf_mult": 0.3},
    "HIGH_VOLATILITY":     {"t1_r": 1.0, "t2_r": 2.0, "t3_r": 3.0, "sl_atr_mult": 2.0, "trigger_buf_mult": 0.4, "sl_struct_buf_mult": 0.4},
    # RANGE / LOW_VOLATILITY: market itself is telling us it won't extend
    # far after a trigger (low ADX, compressed ATR) — take profit sooner so
    # T1/T2 are actually reachable, and keep SL close since the "room" this
    # regime offers is small to begin with.
    "RANGE":               {"t1_r": 0.75, "t2_r": 1.25, "t3_r": 1.5, "sl_atr_mult": 1.0, "trigger_buf_mult": 0.2, "sl_struct_buf_mult": 0.2},
    "LOW_VOLATILITY":      {"t1_r": 0.75, "t2_r": 1.25, "t3_r": 1.5, "sl_atr_mult": 1.0, "trigger_buf_mult": 0.2, "sl_struct_buf_mult": 0.2},
    # Expiry day: fast gamma moves but also fast reversals — tighter targets,
    # shorter assumed hold (theta bites hard).
    "EXPIRY_HIGH_GAMMA":   {"t1_r": 0.75, "t2_r": 1.5,  "t3_r": 2.0, "sl_atr_mult": 1.2, "trigger_buf_mult": 0.25, "sl_struct_buf_mult": 0.25},
    "NO_TRADE":            {"t1_r": 1.0, "t2_r": 2.0, "t3_r": 2.5, "sl_atr_mult": 1.5, "trigger_buf_mult": 0.3, "sl_struct_buf_mult": 0.3},
}
DEFAULT_LEVEL_PROFILE = {"t1_r": 1.0, "t2_r": 2.0, "t3_r": 2.5, "sl_atr_mult": 1.5, "trigger_buf_mult": 0.3, "sl_struct_buf_mult": 0.3}


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
    lot_size: int = resolve_lot_size("NIFTY"),  # COMMON_CONTRACT_LOT_FALLBACK_20260925
    delta: Optional[float] = None,          # real Black-Scholes delta for the chosen strike, if known
    theta_per_day: Optional[float] = None,  # ₹/day decay for the chosen strike, if known (negative)
    assumed_hold_days: Optional[float] = None, # explicit override; otherwise snapshot → 15:30 IST
    regime: Optional[str] = None,           # market_regime.classify_market_regime()'s "regime" string
) -> Dict:
    """
    Structure + ATR அடிப்படையில் trade levels calculate செய்கிறோம்.

    Args:
        market_data: Full market data
        direction: "bullish" (BUY CE) or "bearish" (BUY PE)
        spot: Current NIFTY spot price
        option_ltp: Option premium
        lot_size: Contract lot size; exact chain size should be supplied by caller
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

    # ── Regime-adaptive profile (see REGIME_LEVEL_PROFILES docstring) ─────
    profile = REGIME_LEVEL_PROFILES.get(str(regime or "").upper(), DEFAULT_LEVEL_PROFILE)
    if regime:
        reasons.append(f"Levels adapted for {regime} regime (T2={profile['t2_r']:.2g}R, SL≈{profile['sl_atr_mult']:.2g}×ATR)")

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
            trigger = spot + _atr_buffer(atr, profile["trigger_buf_mult"])
            reasons.append(f"Trigger: NIFTY > {trigger:.0f} (ATR-based breakout)")

        # Entry zone: just above trigger
        entry_low  = trigger
        entry_high = trigger + _atr_buffer(atr, profile["trigger_buf_mult"] * 0.67)

        # SL: nearest support below spot
        near_sup = _nearest_support(spot, supports)
        if near_sup and (spot - near_sup) / spot < 0.025:
            sl_spot  = near_sup - _atr_buffer(atr, profile["sl_struct_buf_mult"])   # buffer below support
            sl_method = f"structure SL at {near_sup:.0f} support − buffer"
        elif vwap > 0:
            sl_spot  = vwap - _atr_buffer(atr, profile["sl_struct_buf_mult"] + 0.2)
            sl_method = f"VWAP {vwap:.0f} − ATR buffer"
        else:
            sl_spot  = spot - atr * profile["sl_atr_mult"]
            sl_method = f"{profile['sl_atr_mult']:.2g}× ATR below entry ({atr:.0f})"

        # Structural SL safety cap: a distant support/resistance level must
        # never create a stop wider than the regime's own ATR risk boundary.
        # The entry trigger is the correct reference for this cap, not spot.
        if atr > 0:
            atr_cap_sl = entry_low - atr * profile["sl_atr_mult"]
            if sl_spot < atr_cap_sl:
                reasons.append(
                    f"Structural SL capped at {profile['sl_atr_mult']:.2g}×ATR from entry"
                )
                sl_spot = atr_cap_sl
                sl_method = (
                    f"ATR-capped structural SL ({profile['sl_atr_mult']:.2g}×ATR)"
                )

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
            trigger = spot - _atr_buffer(atr, profile["trigger_buf_mult"])
            reasons.append(f"Trigger: NIFTY < {trigger:.0f} (ATR-based breakdown)")

        entry_low  = trigger - _atr_buffer(atr, profile["trigger_buf_mult"] * 0.67)
        entry_high = trigger

        # SL: nearest resistance above spot
        near_res = _nearest_resistance(spot, resistances)
        if near_res and (near_res - spot) / spot < 0.025:
            sl_spot  = near_res + _atr_buffer(atr, profile["sl_struct_buf_mult"])
            sl_method = f"structure SL at {near_res:.0f} resistance + buffer"
        elif vwap > 0:
            sl_spot  = vwap + _atr_buffer(atr, profile["sl_struct_buf_mult"] + 0.2)
            sl_method = f"VWAP {vwap:.0f} + ATR buffer"
        else:
            sl_spot  = spot + atr * profile["sl_atr_mult"]
            sl_method = f"{profile['sl_atr_mult']:.2g}× ATR above entry ({atr:.0f})"

        # Structural SL safety cap: a distant support/resistance level must
        # never create a stop wider than the regime's own ATR risk boundary.
        # The entry trigger is the correct reference for this cap, not spot.
        if atr > 0:
            atr_cap_sl = entry_high + atr * profile["sl_atr_mult"]
            if sl_spot > atr_cap_sl:
                reasons.append(
                    f"Structural SL capped at {profile['sl_atr_mult']:.2g}×ATR from entry"
                )
                sl_spot = atr_cap_sl
                sl_method = (
                    f"ATR-capped structural SL ({profile['sl_atr_mult']:.2g}×ATR)"
                )

        reasons.append(f"SL basis: {sl_method}")

    # ── 2. Risk (underlying) ──────────────────────────────────────────────
    if direction == "bullish":
        risk_spot = abs(entry_low - sl_spot)
    else:
        risk_spot = abs(sl_spot - entry_high)

    if risk_spot <= 0:
        risk_spot = atr * 1.0   # fallback

    # ── 3. Targets (regime-adaptive R-multiples — see REGIME_LEVEL_PROFILES) ─
    t1_r, t2_r, t3_r = profile["t1_r"], profile["t2_r"], profile["t3_r"]
    if direction == "bullish":
        t1_spot = entry_low + risk_spot * t1_r
        t2_spot = entry_low + risk_spot * t2_r
        t3_spot = entry_low + risk_spot * t3_r
    else:
        t1_spot = entry_high - risk_spot * t1_r
        t2_spot = entry_high - risk_spot * t2_r
        t3_spot = entry_high - risk_spot * t3_r

    rr_ratio = round(t2_r, 2)  # T2 defines the quoted R:R for this regime

    # ── 4. Option premium levels ────────────────────────────────────────────
    # Fallback constant only used when a real per-strike delta isn't
    # available — near-ATM options move roughly ₹0.45 per ₹1 of spot, but
    # this is now a LAST RESORT, not the default (see docstring).
    DELTA_APPROX_FALLBACK = 0.45
    eff_delta = abs(delta) if delta is not None else DELTA_APPROX_FALLBACK
    delta_is_real = delta is not None

    # AUTHORITATIVE_INTRADAY_THETA_TIME_20260925:
    # Use the actual snapshot timestamp → today's 15:30 IST close.
    # No arbitrary 0.25/0.5/1.0-day assumption.
    if assumed_hold_days is None:
        hold_days = intraday_hold_days_to_close(market_data.get("timestamp"))
    else:
        hold_days = max(0.0, float(assumed_hold_days))

    if option_ltp > 0:
        opt_risk = round(risk_spot * eff_delta, 1)
        opt_sl   = round(option_ltp - opt_risk, 1)
        opt_t1   = round(option_ltp + opt_risk * t1_r, 1)
        opt_t2   = round(option_ltp + opt_risk * t2_r, 1)
        opt_t3   = round(option_ltp + opt_risk * t3_r, 1)

        # THETA_TARGET_PRESERVE_FIX:
        # Keep valid pre-theta targets available when theta adjustment
        # alone would make T1/T2/T3 ordering invalid.
        pre_theta_t1 = opt_t1
        pre_theta_t2 = opt_t2
        pre_theta_t3 = opt_t3
        pre_theta_targets_valid = (
            pre_theta_t1 > option_ltp
            and pre_theta_t2 > pre_theta_t1
            and pre_theta_t3 > pre_theta_t2
        )

        # Net theta decay OUT of the targets (review #23/#38: a
        # directionally-correct trade can still lose money to time decay —
        # a target that ignores this overstates what's realistically
        # reachable). SL gets a little decay cushion too (needs a slightly
        # bigger adverse move to trigger, since decay is already working
        # against the position independent of direction).
        theta_note = ""
        if theta_per_day is not None and theta_per_day < 0 and hold_days > 0:
            expected_decay = round(abs(theta_per_day) * hold_days, 1)

            # BUY_THETA_SIGN_FIX_20260925:
            # Theta decay works AGAINST a long option.  Therefore the
            # premium target must require an EXTRA move to preserve the
            # intended structural reward.  Subtracting theta from a BUY
            # target falsely moves the target closer to entry and can turn
            # a 1R target into a tiny reward (e.g. ₹19.6 risk vs ₹1.6 T1).
            opt_t1 = round(opt_t1 + expected_decay, 1)
            opt_t2 = round(opt_t2 + expected_decay, 1)
            opt_t3 = round(opt_t3 + expected_decay, 1)

            # SL remains cushioned for time decay: some premium erosion can
            # occur even without an adverse underlying move.
            opt_sl = round(opt_sl - expected_decay * 0.5, 1)
            theta_note = f", requires ~₹{expected_decay:.1f} extra premium move for expected theta decay over {hold_days:.2f}d"

        # Premium SL floor: never let option go to zero
        premium_sl_floor = round(option_ltp * 0.40, 1)
        opt_sl = max(opt_sl, premium_sl_floor)

        # Final option-premium ordering validation.
        # Option levels must stay entirely in premium space:
        # SL < Entry < T1 < T2 < T3.
        # Theta adjustment can otherwise push a target below entry.
        option_order_valid = (
            opt_sl < option_ltp
            and opt_t1 > option_ltp
            and opt_t2 > opt_t1
            and opt_t3 > opt_t2
        )
        if not option_order_valid:
            if pre_theta_targets_valid:
                reasons.append(
                    "Theta adjustment invalidated targets — preserved valid pre-theta T1/T2/T3"
                )
                opt_t1 = pre_theta_t1
                opt_t2 = pre_theta_t2
                opt_t3 = pre_theta_t3
            else:
                reasons.append("Invalid option target order after theta adjustment")
                opt_t1 = opt_t2 = opt_t3 = None

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
    # BUG FIX (2026-08-22): this referenced an undefined name `DELTA_APPROX`
    # (only `DELTA_APPROX_FALLBACK` exists) — any call with option_ltp <= 0
    # would raise NameError instead of returning a usable fallback.
    risk_per_lot = round(opt_risk * lot_size, 0) if opt_risk > 0 else round(risk_spot * DELTA_APPROX_FALLBACK * lot_size, 0)

    # ── 6. Setup quality ──────────────────────────────────────────────────
    # BUG FIX (2026-08-22): this used to grab reasons[1] positionally to find
    # the "SL basis: ..." line — inserting the regime note above shifted that
    # index. Search by content instead so it can't silently break again.
    sl_basis_reason = next((r for r in reasons if r.startswith("SL basis")), "")
    has_structure_sl = any(s in sl_basis_reason for s in ["structure", "VWAP", "support", "resistance"])
    # Regime-adaptive minimum/preferred R:R — a RANGE/LOW_VOL regime's own
    # T2 (e.g. 1.25R) IS the realistic ceiling for that regime, so grading it
    # against a flat 2.0R "preferred" would mislabel every range-day setup
    # as LOW quality even when it matches what the regime can deliver.
    regime_min_rr = min(MIN_RR, t2_r)
    regime_preferred_rr = min(PREFERRED_RR, t2_r)
    if rr_ratio >= regime_preferred_rr and has_structure_sl and atr_pct > 0:
        quality = "HIGH"
    elif rr_ratio >= regime_min_rr:
        quality = "MEDIUM"
    else:
        quality = "LOW"
        reasons.append(f"⚠️ R:R {rr_ratio:.1f} below minimum {regime_min_rr:.2g} — consider skipping")

    # Actual option-premium R:R at T2.
    # Keep rr_ratio unchanged: it is the regime/spot-model R:R used
    # by setup-quality and risk logic.
    option_rr_ratio = None
    if option_ltp > 0 and opt_sl is not None and opt_t2 is not None:
        option_risk_actual = option_ltp - opt_sl
        option_reward_actual = opt_t2 - option_ltp
        if option_risk_actual > 0 and option_reward_actual > 0:
            option_rr_ratio = round(
                option_reward_actual / option_risk_actual, 2
            )

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
        "option_rr_ratio":  option_rr_ratio,
        "setup_quality":   quality,
        "atr":             round(atr, 1),
        "atr_pct":         round(atr_pct, 2),
        "reasons":         reasons,
        "lot_size":        lot_size,
    }
