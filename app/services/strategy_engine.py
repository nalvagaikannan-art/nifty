"""
StrategyEngine — Concrete option strategy suggestions
=======================================================
decision_engine.py already picks a strategy *name* (Directional Call Buying,
Long Straddle/Strangle, Iron Condor, etc.) from trend/volatility conditions.
This module fills that name in with real numbers from the live option chain
snapshot already fetched this request: which strike(s), current premium,
stop-loss, target, and — for multi-leg ideas — the exact legs, net
premium/credit, breakeven points, and max profit/loss.

⚠️ Suggestion only. Nothing here places an order or talks to a broker —
it only computes numbers from data already in hand. SL/target percentages
for single-leg buying are standard retail heuristics (not derived from the
chain, since live per-strike greeks/delta aren't available here) and are
spelled out in the reasoning text so they're easy to override.
"""
from typing import Dict, List, Optional
import pandas as pd
from app.utils.helpers import safe_float
from app.services.contract_specs import resolve_lot_size

# ── Single-leg directional buying — retail rule-of-thumb risk management ──
SINGLE_LEG_SL_PCT     = 0.35   # stop-loss at 35% premium erosion
SINGLE_LEG_TARGET_PCT = 0.60   # target at 60% premium gain (~1.7:1 reward:risk)

# ── Strike offsets for multi-leg strategies (approx % away from spot) ─────
STRANGLE_OTM_PCT     = 0.015   # ~1.5% OTM for strangle legs
CONDOR_SHORT_OTM_PCT = 0.015   # short strikes ~1.5% OTM
CONDOR_WING_OTM_PCT  = 0.035   # long wings ~3.5% OTM (defines max loss)


def _nearest_row(df: pd.DataFrame, target_strike: float) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    idx = (df["strike"] - target_strike).abs().idxmin()
    return df.loc[idx]


def _leg(row: pd.Series, side: str, action: str, expiry: str = "") -> Dict:
    """side: 'ce' or 'pe'; action: 'BUY' or 'SELL'.
    expiry: this request's resolved live-chain expiry (same one every row
    in opt_df came from) — attached here so the frontend can always show
    the complete contract (strike + type + expiry), never just the strike."""
    return {
        "action":  action,
        "type":    side.upper(),
        "strike":  float(row["strike"]),
        "expiry":  expiry,
        "premium": safe_float(row.get(f"{side}_ltp", 0)),
    }


def _single_leg(
    df: pd.DataFrame,
    spot: float,
    side: str,
    expiry: str = "",
    lot_size: int = 1,
) -> Optional[Dict]:
    row = _nearest_row(df, spot)
    if row is None:
        return None

    premium = safe_float(row.get(f"{side}_ltp", 0))
    if premium <= 0:
        return None

    sl = round(premium * (1 - SINGLE_LEG_SL_PCT), 1)
    target = round(premium * (1 + SINGLE_LEG_TARGET_PCT), 1)

    # STRATEGY_LOT_VALUE_FIX_20260925
    # Option premium is quoted per index unit. Convert only the risk
    # amount to ₹/lot using the current contract lot size.
    max_loss_points = round(premium - sl, 1)
    max_loss_per_lot = round(max_loss_points * lot_size, 2)

    expiry_note = f" ({expiry} expiry)" if expiry else ""

    return {
        "name":               f"ATM {side.upper()} Buying",
        "legs":               [_leg(row, side, "BUY", expiry)],
        "entry_premium":      premium,
        "stop_loss":          sl,
        "target":             target,
        "max_loss_points":    max_loss_points,
        "max_loss_per_lot":   max_loss_per_lot,
        "lot_size":           lot_size,
        "reasoning": (
            f"BUY {row['strike']:.0f} {side.upper()}{expiry_note} @ ₹{premium:.1f} LTP. "
            f"Stop-loss ₹{sl:.1f} ({int(SINGLE_LEG_SL_PCT*100)}% premium erosion), "
            f"target ₹{target:.1f} ({int(SINGLE_LEG_TARGET_PCT*100)}% gain). "
            f"Max modeled loss = {max_loss_points:.1f} premium points "
            f"= ₹{max_loss_per_lot:.2f}/lot. "
            f"These SL/target percentages are standard retail option-buying "
            f"heuristics, not derived from live greeks."
        ),
    }

def _straddle_or_strangle(
    df: pd.DataFrame,
    spot: float,
    kind: str,
    expiry: str = "",
    lot_size: int = 1,
) -> Optional[Dict]:
    if kind == "straddle":
        ce_row = _nearest_row(df, spot)
        pe_row = ce_row
    else:
        ce_row = _nearest_row(df, spot * (1 + STRANGLE_OTM_PCT))
        pe_row = _nearest_row(df, spot * (1 - STRANGLE_OTM_PCT))

    if ce_row is None or pe_row is None:
        return None

    ce_prem = safe_float(ce_row.get("ce_ltp", 0))
    pe_prem = safe_float(pe_row.get("pe_ltp", 0))

    if ce_prem <= 0 or pe_prem <= 0:
        return None

    net_debit_points = round(ce_prem + pe_prem, 1)
    net_debit_per_lot = round(net_debit_points * lot_size, 2)

    upper_be = round(
        float(ce_row["strike"]) + net_debit_points, 1
    )
    lower_be = round(
        float(pe_row["strike"]) - net_debit_points, 1
    )

    expiry_note = f" ({expiry} expiry)" if expiry else ""

    return {
        "name":               "Long Straddle" if kind == "straddle" else "Long Strangle",
        "legs":               [
            _leg(ce_row, "ce", "BUY", expiry),
            _leg(pe_row, "pe", "BUY", expiry),
        ],
        "net_debit":          net_debit_points,
        "net_debit_points":   net_debit_points,
        "net_debit_per_lot":  net_debit_per_lot,
        "max_loss":           net_debit_points,
        "max_loss_points":    net_debit_points,
        "max_loss_per_lot":   net_debit_per_lot,
        "lot_size":           lot_size,
        "breakeven_upper":    upper_be,
        "breakeven_lower":    lower_be,
        "reasoning": (
            f"BUY {ce_row['strike']:.0f} CE @ ₹{ce_prem:.1f} + "
            f"BUY {pe_row['strike']:.0f} PE @ ₹{pe_prem:.1f}{expiry_note} "
            f"= net debit {net_debit_points:.1f} points = "
            f"₹{net_debit_per_lot:.2f}/lot. "
            f"This is also the maximum premium loss at expiry. "
            f"Breakevens are {lower_be:.1f} and {upper_be:.1f} index points."
        ),
    }

def _iron_condor(
    df: pd.DataFrame,
    spot: float,
    expiry: str = "",
    lot_size: int = 1,
) -> Optional[Dict]:
    short_ce = _nearest_row(
        df, spot * (1 + CONDOR_SHORT_OTM_PCT)
    )
    long_ce = _nearest_row(
        df, spot * (1 + CONDOR_WING_OTM_PCT)
    )
    short_pe = _nearest_row(
        df, spot * (1 - CONDOR_SHORT_OTM_PCT)
    )
    long_pe = _nearest_row(
        df, spot * (1 - CONDOR_WING_OTM_PCT)
    )

    if any(r is None for r in (short_ce, long_ce, short_pe, long_pe)):
        return None

    # A collapsed wing is not a valid condor.
    if (
        float(short_ce["strike"]) == float(long_ce["strike"])
        or float(short_pe["strike"]) == float(long_pe["strike"])
    ):
        return None

    sc = safe_float(short_ce.get("ce_ltp", 0))
    lc = safe_float(long_ce.get("ce_ltp", 0))
    sp = safe_float(short_pe.get("pe_ltp", 0))
    lp = safe_float(long_pe.get("pe_ltp", 0))

    if sc <= 0 or sp <= 0:
        return None

    # STRATEGY_LOT_VALUE_FIX_20260925
    # Premium calculations are in option-price points. Only the monetary
    # totals are multiplied by the exact contract lot size.
    net_credit_points = round(
        (sc - lc) + (sp - lp), 1
    )

    ce_wing = abs(
        float(long_ce["strike"]) - float(short_ce["strike"])
    )
    pe_wing = abs(
        float(short_pe["strike"]) - float(long_pe["strike"])
    )

    max_loss_points = round(
        max(ce_wing, pe_wing) - net_credit_points,
        1,
    )

    net_credit_per_lot = round(
        net_credit_points * lot_size,
        2,
    )
    max_loss_per_lot = round(
        max_loss_points * lot_size,
        2,
    )

    breakeven_upper = round(
        float(short_ce["strike"]) + net_credit_points,
        1,
    )
    breakeven_lower = round(
        float(short_pe["strike"]) - net_credit_points,
        1,
    )

    expiry_note = f" ({expiry} expiry)" if expiry else ""

    return {
        "name": "Iron Condor",
        "legs": [
            _leg(short_ce, "ce", "SELL", expiry),
            _leg(long_ce, "ce", "BUY", expiry),
            _leg(short_pe, "pe", "SELL", expiry),
            _leg(long_pe, "pe", "BUY", expiry),
        ],

        # Premium-point values
        "lot_size":           lot_size,
        "net_credit":         net_credit_points,
        "net_credit_points":  net_credit_points,
        "max_profit":         net_credit_points,
        "max_profit_points":  net_credit_points,
        "max_loss":           max_loss_points,
        "max_loss_points":    max_loss_points,

        # Actual ₹ amount for one complete contract lot
        "net_credit_per_lot":  net_credit_per_lot,
        "max_profit_per_lot":  net_credit_per_lot,
        "max_loss_per_lot":    max_loss_per_lot,

        # Underlying index levels — DO NOT multiply by lot size
        "breakeven_upper":     breakeven_upper,
        "breakeven_lower":     breakeven_lower,

        "reasoning": (
            f"SELL {short_ce['strike']:.0f} CE @ ₹{sc:.1f} / "
            f"BUY {long_ce['strike']:.0f} CE @ ₹{lc:.1f} "
            f"(call wing) + SELL {short_pe['strike']:.0f} PE @ ₹{sp:.1f} / "
            f"BUY {long_pe['strike']:.0f} PE @ ₹{lp:.1f} "
            f"(put wing){expiry_note} = net credit "
            f"{net_credit_points:.1f} points = ₹{net_credit_per_lot:.2f}/lot. "
            f"Max profit ₹{net_credit_per_lot:.2f}/lot if spot stays "
            f"between the short strikes at expiry; max loss "
            f"{max_loss_points:.1f} points = ₹{max_loss_per_lot:.2f}/lot "
            f"if spot breaks past a wing."
        ),
    }

def generate_price_levels(market_data: Dict) -> Optional[Dict]:
    """
    Spot-level Entry Zone / Target 1 / Target 2 / Stop-Loss / Invalidation
    Level — the price "zones" a bias plays out in, as distinct from
    generate_option_strategy()'s strike/premium numbers above.

    Purely ATR + support/resistance based (both already computed in
    market_data — no new data source needed):
      - entry_zone: a tight band around current spot (± 0.15×ATR) — the
        "if price is trading here, the bias is still live" window.
      - target_1 / target_2: 0.5×ATR / 1.0×ATR beyond spot in the bias
        direction — i.e. half and full of today's typical range.
      - stop_loss: 0.3×ATR against the bias direction.
      - invalidation_level: the nearest support (bullish bias) or
        resistance (bearish bias) — if spot closes through this, the
        underlying premise (not just the SL) is considered broken.

    Returns None when preferred_side is "NONE" (no directional bias to
    build zones around) or ATR/spot aren't available yet.
    """
    decision = market_data.get("decision", {})
    side = decision.get("preferred_side", "NONE")
    if side not in ("CALL", "PUT"):
        return None

    spot = market_data.get("spot", {}).get("price", 0)
    atr  = market_data.get("technicals", {}).get("atr", 0)
    if spot <= 0 or atr <= 0:
        return None

    sr = market_data.get("support_resistance", {})
    supports    = sr.get("support", [])
    resistances = sr.get("resistance", [])

    entry_buffer = round(atr * 0.15, 1)
    entry_zone = {"low": round(spot - entry_buffer, 1), "high": round(spot + entry_buffer, 1)}

    if side == "CALL":
        target_1 = round(spot + atr * 0.5, 1)
        target_2 = round(spot + atr * 1.0, 1)
        stop_loss = round(spot - atr * 0.3, 1)
        invalidation_level = round(supports[0], 1) if supports else stop_loss
    else:  # PUT
        target_1 = round(spot - atr * 0.5, 1)
        target_2 = round(spot - atr * 1.0, 1)
        stop_loss = round(spot + atr * 0.3, 1)
        invalidation_level = round(resistances[0], 1) if resistances else stop_loss

    return {
        "entry_zone":         entry_zone,
        "target_1":           target_1,
        "target_2":           target_2,
        "stop_loss":          stop_loss,
        "invalidation_level": invalidation_level,
        "basis": (
            f"ATR-based zones (ATR≈{atr:.1f} points). Invalidation = nearest "
            f"{'support' if side == 'CALL' else 'resistance'} level — a close "
            f"beyond this means the bias premise itself is broken, not just the SL."
        ),
    }


def generate_option_strategy(
    market_data: Dict,
    opt_df: pd.DataFrame,
) -> Optional[Dict]:
    """
    Fill the decision engine's chosen strategy with concrete strikes and
    premiums from the already-fetched live option chain.

    Monetary totals explicitly distinguish:
      - premium/index-option points
      - ₹ per contract lot

    Suggestion only; no order placement.
    """
    if opt_df is None or opt_df.empty:
        return None

    decision_block = market_data.get("decision", {})
    strategy_name = decision_block.get("strategy", "")

    spot = market_data.get("spot", {}).get("price", 0)
    if spot <= 0:
        return None

    option_chain = market_data.get("option_chain") or {}

    # STRATEGY_LOT_VALUE_FIX_20260925
    # Prefer exact lotsize carried from Angel instrument master.
    # contract_specs supplies current fallback for providers that don't expose it.
    lot_size = resolve_lot_size(
        str(
            option_chain.get("symbol")
            or market_data.get("symbol")
            or "NIFTY"
        ).upper(),
        option_chain,
    )

    if lot_size <= 0:
        return None

    expiry = option_chain.get("expiry", "")

    if strategy_name == "Directional Call Bias":
        return _single_leg(
            opt_df, spot, "ce", expiry, lot_size
        )

    if strategy_name == "Directional Put Bias":
        return _single_leg(
            opt_df, spot, "pe", expiry, lot_size
        )

    if strategy_name.startswith("Weak-Trend"):
        side = (
            "ce"
            if " CE " in f" {strategy_name} "
            else "pe"
        )
        result = _single_leg(
            opt_df, spot, side, expiry, lot_size
        )
        if result:
            result["note"] = (
                "Trend is weak (ADX < 20) — smaller size reduces "
                "exposure to a range-bound whipsaw."
            )
        return result

    if strategy_name == "Long Straddle / Strangle":
        return (
            _straddle_or_strangle(
                opt_df, spot, "strangle", expiry, lot_size
            )
            or _straddle_or_strangle(
                opt_df, spot, "straddle", expiry, lot_size
            )
        )

    if strategy_name.startswith("Range Strategy"):
        return _iron_condor(
            opt_df, spot, expiry, lot_size
        )

    # NEUTRAL_RANGE_IC_FALLBACK_20260925
    # A genuine RANGE/LOW_VOLATILITY market can still have the
    # neutral textual strategy "No Clear Edge — Sideways".
    if strategy_name == "No Clear Edge — Sideways":
        regime = str(
            decision_block.get("market_regime", "")
        ).upper()
        preferred_side = str(
            decision_block.get("preferred_side", "NONE")
        ).upper()
        margin = abs(
            safe_float(decision_block.get("margin", 0))
        )
        market_open = bool(
            market_data.get("spot", {}).get(
                "market_open", True
            )
        )
        no_trade = bool(
            decision_block.get(
                "market_regime_no_trade", False
            )
        )

        if (
            market_open
            and not no_trade
            and regime in ("RANGE", "LOW_VOLATILITY")
            and preferred_side == "NONE"
            and margin < 8
        ):
            result = _iron_condor(
                opt_df, spot, expiry, lot_size
            )
            if result:
                result["note"] = (
                    "Neutral RANGE/LOW_VOLATILITY setup — "
                    "four-leg Iron Condor built from the live option chain."
                )
            return result

    return None

