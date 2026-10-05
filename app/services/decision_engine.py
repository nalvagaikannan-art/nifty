"""
Rule-Based Decision Engine
===========================
20 conditions score செய்து Bull/Bear/Neutral score கணக்கிடும்.
AI இந்த score-ஐ verify செய்து reasoning மட்டும் சொல்லும்.
"""

from typing import Dict, List, Tuple, Optional
import logging
import time
from datetime import datetime, timezone

from app.services.market_regime import classify_market_regime

logger = logging.getLogger(__name__)

# ── Signal hysteresis (BUG FIX 2026-08-24) ──────────────────────────────────
# run_decision_engine() used to be a pure function re-scored from scratch on
# every call with NO memory of the previous signal. margin is a sum of
# discrete per-indicator points, and preferred_side flips to CALL/PUT only
# once margin crosses ±10 — so a single indicator ticking across ITS OWN
# threshold on a routine tick (spot crossing EMA20 by a point, RSI crossing
# 50, one OI print updating) could swing margin across that ±10 line and
# flip CALL → NONE → PUT → NONE on successive 30s dashboard refreshes, even
# though nothing about the actual market changed materially. That's what
# was reported as "signal changes frequently, wrong signal".
# Fix: a classic hysteresis (Schmitt-trigger) band, per symbol. ENTERING a
# CALL/PUT signal still needs the full ±10 margin (unchanged, deliberately
# conservative). But once a signal IS showing CALL/PUT, it's only given up
# when margin weakens past a much softer ±HYSTERESIS_EXIT_MARGIN — so a
# signal that's "still mostly right, just less strongly so" keeps being
# shown instead of flickering to NONE and back on every refresh. A genuine
# reversal (margin swinging past the softer band) still updates promptly.
_signal_state: Dict[str, Dict] = {}
HYSTERESIS_EXIT_MARGIN = 4

# Signal lifecycle / confirmation settings.
# A raw directional score is NOT an entry by itself. The same direction must
# be seen on consecutive analysis cycles before it becomes CONFIRMED.
SIGNAL_CONFIRMATIONS_REQUIRED = 3
SIGNAL_REVERSAL_CONFIRMATIONS_REQUIRED = 2
SIGNAL_CONFIRMATION_MIN_INTERVAL_SECONDS = 45
# A direction must have a meaningful score margin, not merely cross the
# old ±10 preferred-side threshold, before it can become an entry confirmation.
SIGNAL_CONFIRMATION_MIN_MARGIN = 12
SIGNAL_HOLD_MIN_MARGIN = 12
SIGNAL_REVERSAL_MIN_MARGIN = 12

def _apply_signal_lifecycle(
    symbol_key: str,
    raw_side: str,
    margin: float,
    hard_gated: bool,
) -> Tuple[str, Dict]:
    """Turn raw CALL/PUT/NONE readings into WATCH/CONFIRMED/HOLD states.

    HOLD is only allowed while the currently active side still has
    meaningful supporting evidence.  A weak active reading is released
    instead of being carried indefinitely by hysteresis.
    """
    now = time.time()
    prev = _signal_state.get(symbol_key, {})
    prev_active = prev.get("active_side", "NONE")
    prev_candidate = prev.get("candidate_side", "NONE")
    confirmations = int(prev.get("confirmations", 0) or 0)
    reversal_confirmations = int(prev.get("reversal_confirmations", 0) or 0)
    last_confirmation_ts = float(prev.get("last_confirmation_ts", 0) or 0)

    can_count_confirmation = (
        last_confirmation_ts <= 0
        or (now - last_confirmation_ts) >= SIGNAL_CONFIRMATION_MIN_INTERVAL_SECONDS
    )

    # ------------------------------------------------------------------
    # Hard safety gate: immediate reset.
    # ------------------------------------------------------------------
    if hard_gated:
        return "NONE", {
            "state": "WAIT",
            "candidate_side": "NONE",
            "confirmations": 0,
            "reversal_confirmations": 0,
            "active_side": "NONE",
            "changed": prev_active != "NONE",
            "reason": "Hard safety gate is active; no directional signal is allowed.",
            "last_confirmation_ts": last_confirmation_ts,
            "ts": now,
        }

    # ------------------------------------------------------------------
    # Raw NONE: an active signal must be released.
    # Do NOT keep stale HOLD_CALL/HOLD_PUT when there is no direction.
    # ------------------------------------------------------------------
    if raw_side not in ("CALL", "PUT"):
        return "NONE", {
            "state": "WAIT",
            "candidate_side": "NONE",
            "confirmations": 0,
            "reversal_confirmations": 0,
            "active_side": "NONE",
            "changed": prev_active != "NONE",
            "reason": "No confirmed directional signal; active signal released.",
            "last_confirmation_ts": last_confirmation_ts,
            "ts": now,
        }

    # ------------------------------------------------------------------
    # No active signal: build a fresh confirmation sequence.
    # ------------------------------------------------------------------
    if prev_active not in ("CALL", "PUT"):
        if abs(margin) < SIGNAL_CONFIRMATION_MIN_MARGIN:
            return "NONE", {
                "state": f"WATCH_{raw_side}",
                "candidate_side": raw_side,
                "confirmations": 0,
                "reversal_confirmations": 0,
                "active_side": "NONE",
                "changed": False,
                "reason": (
                    f"{raw_side} direction is not strong enough yet "
                    f"(margin {margin:.1f} < {SIGNAL_CONFIRMATION_MIN_MARGIN})."
                ),
                "last_confirmation_ts": last_confirmation_ts,
                "ts": now,
            }

        if raw_side == prev_candidate:
            if can_count_confirmation:
                confirmations += 1
                last_confirmation_ts = now
        else:
            prev_candidate = raw_side
            confirmations = 1
            last_confirmation_ts = now

        if confirmations >= SIGNAL_CONFIRMATIONS_REQUIRED:
            return raw_side, {
                "state": f"CONFIRMED_{raw_side}",
                "candidate_side": raw_side,
                "confirmations": confirmations,
                "reversal_confirmations": 0,
                "active_side": raw_side,
                "changed": True,
                "reason": (
                    f"{raw_side} confirmed after "
                    f"{confirmations} consecutive readings."
                ),
                "last_confirmation_ts": last_confirmation_ts,
                "ts": now,
            }

        return "NONE", {
            "state": f"WATCH_{raw_side}",
            "candidate_side": raw_side,
            "confirmations": confirmations,
            "reversal_confirmations": 0,
            "active_side": "NONE",
            "changed": False,
            "reason": (
                f"Waiting for "
                f"{SIGNAL_CONFIRMATIONS_REQUIRED - confirmations} "
                f"more confirmation cycle(s)."
            ),
            "last_confirmation_ts": last_confirmation_ts,
            "ts": now,
        }

    # ------------------------------------------------------------------
    # Active signal: first verify that the ACTIVE direction itself is
    # still supported by a meaningful margin.
    #
    # CALL requires +12 or more.
    # PUT  requires -12 or less.
    #
    # This is the critical HOLD revalidation.
    # ------------------------------------------------------------------
    active_evidence_strong = (
        (prev_active == "CALL" and margin >= SIGNAL_HOLD_MIN_MARGIN)
        or
        (prev_active == "PUT" and margin <= -SIGNAL_HOLD_MIN_MARGIN)
    )

    # Same raw side.
    if raw_side == prev_active:
        if not active_evidence_strong:
            return "NONE", {
                "state": f"WATCH_{raw_side}",
                "candidate_side": raw_side,
                "confirmations": 0,
                "reversal_confirmations": 0,
                "active_side": "NONE",
                "changed": True,
                "reason": (
                    f"Active {prev_active} released because its supporting "
                    f"margin is weak ({margin:.1f}); fresh confirmation required."
                ),
                "last_confirmation_ts": last_confirmation_ts,
                "ts": now,
            }

        return prev_active, {
            "state": f"HOLD_{prev_active}",
            "candidate_side": raw_side,
            "confirmations": max(confirmations, SIGNAL_CONFIRMATIONS_REQUIRED),
            "reversal_confirmations": 0,
            "active_side": prev_active,
            "changed": False,
            "reason": (
                f"{prev_active} remains confirmed with supporting "
                f"margin {margin:.1f}."
            ),
            "last_confirmation_ts": last_confirmation_ts,
            "ts": now,
        }

    # ------------------------------------------------------------------
    # Opposite raw side.
    #
    # If the active side is already weak AND the opposite side is also not
    # strong enough for reversal, release to WATCH instead of stale HOLD.
    #
    # If the opposite side IS strong enough, retain the existing
    # two-confirmation reversal mechanism.
    # ------------------------------------------------------------------
    opposite_margin_ok = (
        (raw_side == "CALL" and margin >= SIGNAL_REVERSAL_MIN_MARGIN)
        or
        (raw_side == "PUT" and margin <= -SIGNAL_REVERSAL_MIN_MARGIN)
    )

    if not active_evidence_strong and not opposite_margin_ok:
        return "NONE", {
            "state": f"WATCH_{raw_side}",
            "candidate_side": raw_side,
            "confirmations": 0,
            "reversal_confirmations": 0,
            "active_side": "NONE",
            "changed": True,
            "reason": (
                f"Active {prev_active} released: its supporting margin "
                f"is weak ({margin:.1f}) and opposite {raw_side} is not "
                f"strong enough for reversal."
            ),
            "last_confirmation_ts": last_confirmation_ts,
            "ts": now,
        }

    # Opposite side is strong enough: existing reversal confirmation logic.
    if not opposite_margin_ok:
        return prev_active, {
            "state": f"HOLD_{prev_active}",
            "candidate_side": raw_side,
            "confirmations": max(confirmations, SIGNAL_CONFIRMATIONS_REQUIRED),
            "reversal_confirmations": 0,
            "active_side": prev_active,
            "changed": False,
            "reason": (
                f"Opposite {raw_side} reading is too weak for reversal "
                f"(margin {margin:.1f}); {prev_active} remains supported."
            ),
            "last_confirmation_ts": last_confirmation_ts,
            "ts": now,
        }

    if prev_candidate == raw_side:
        if can_count_confirmation:
            reversal_confirmations += 1
            last_confirmation_ts = now
    else:
        prev_candidate = raw_side
        reversal_confirmations = 1
        last_confirmation_ts = now

    if reversal_confirmations >= SIGNAL_REVERSAL_CONFIRMATIONS_REQUIRED:
        return raw_side, {
            "state": f"CONFIRMED_{raw_side}",
            "candidate_side": raw_side,
            "confirmations": SIGNAL_CONFIRMATIONS_REQUIRED,
            "reversal_confirmations": reversal_confirmations,
            "active_side": raw_side,
            "changed": True,
            "reason": (
                f"Reversal to {raw_side} confirmed after "
                f"{reversal_confirmations} consecutive readings."
            ),
            "last_confirmation_ts": last_confirmation_ts,
            "ts": now,
        }

    return prev_active, {
        "state": f"HOLD_{prev_active}_REVERSAL_WATCH_{raw_side}",
        "candidate_side": raw_side,
        "confirmations": max(confirmations, SIGNAL_CONFIRMATIONS_REQUIRED),
        "reversal_confirmations": reversal_confirmations,
        "active_side": prev_active,
        "changed": False,
        "reason": (
            f"Possible {raw_side} reversal detected; holding "
            f"{prev_active} until confirmation."
        ),
        "last_confirmation_ts": last_confirmation_ts,
        "ts": now,
    }


def apply_persistent_signal_lifecycle(
    decision: Dict,
    persisted_state: Optional[Dict],
    now: float,
) -> Tuple[Dict, Dict]:
    """Apply the restart-safe lifecycle stored by the strategy router.

    HOLD is allowed only while the currently active side still has
    meaningful supporting evidence. Weak active evidence releases the
    signal and requires fresh confirmation.
    """
    state = dict(persisted_state or {})
    raw_side = str(
        decision.get("raw_preferred_side", decision.get("preferred_side", "NONE"))
    ).upper()
    margin = float(decision.get("margin", 0) or 0)
    market_open = bool(decision.get("market_open", True))
    hard_gated = (
        bool(decision.get("hard_gated", False))
        or bool(decision.get("market_regime_no_trade", False))
        or not market_open
    )

    active = state.get("active_side", "NONE")
    candidate = state.get("candidate_side", "NONE")
    confirmations = int(state.get("confirmations", 0) or 0)
    reversal_confirmations = int(state.get("reversal_confirmations", 0) or 0)
    last_eval = state.get("last_evaluation_at")
    last_confirm = state.get("last_confirmation_at")

    def epoch(value):
        if value is None:
            return 0.0
        if hasattr(value, "timestamp"):
            if getattr(value, "tzinfo", None) is None:
                value = value.replace(tzinfo=timezone.utc)
            return float(value.timestamp())
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    # SNAPSHOT_DEDUP_FIX_20260923
    # Several browser pages can evaluate the exact same cached market
    # snapshot. Such a snapshot is one observation, not several
    # confirmations. Older in-flight snapshots must also be ignored.
    _incoming_eval_epoch = float(now or 0)
    _stored_eval_epoch = epoch(last_eval)

    if (
        _stored_eval_epoch > 0
        and _incoming_eval_epoch > 0
        and _incoming_eval_epoch <= _stored_eval_epoch
    ):
        lifecycle = str(state.get("lifecycle", "WAIT") or "WAIT")
        active = str(state.get("active_side", "NONE") or "NONE").upper()
        candidate = str(state.get("candidate_side", "NONE") or "NONE").upper()
        confirmations = int(state.get("confirmations", 0) or 0)
        reversal_confirmations = int(
            state.get("reversal_confirmations", 0) or 0
        )

        out = dict(decision)
        out["signal_lifecycle"] = lifecycle
        out["signal_candidate"] = candidate
        out["signal_confirmations"] = confirmations
        out["signal_reversal_confirmations"] = reversal_confirmations
        out["signal_active_side"] = active
        out["signal_lifecycle_reason"] = (
            "Duplicate/older market snapshot ignored; lifecycle state preserved."
        )

        if lifecycle.startswith(("CONFIRMED_", "HOLD_")) and active in ("CALL", "PUT"):
            out["preferred_side"] = active
        else:
            out["preferred_side"] = "NONE"

        return out, state

    can_count = (
        _stored_eval_epoch <= 0
        or _incoming_eval_epoch - _stored_eval_epoch
        >= SIGNAL_CONFIRMATION_MIN_INTERVAL_SECONDS
    )

    # ------------------------------------------------------------------
    # Hard safety gate.
    # ------------------------------------------------------------------
    if hard_gated:
        active = "NONE"
        candidate = "NONE"
        confirmations = 0
        reversal_confirmations = 0
        lifecycle = "WAIT"
        reason = "Hard safety gate is active; no directional signal is allowed."

    # ------------------------------------------------------------------
    # No raw direction: release any active signal.
    # ------------------------------------------------------------------
    elif raw_side not in ("CALL", "PUT"):
        active = "NONE"
        candidate = "NONE"
        confirmations = 0
        reversal_confirmations = 0
        lifecycle = "WAIT"
        reason = "No confirmed directional signal; active signal released."

    # ------------------------------------------------------------------
    # No active signal: fresh confirmation sequence.
    # ------------------------------------------------------------------
    elif active not in ("CALL", "PUT"):
        if abs(margin) < SIGNAL_CONFIRMATION_MIN_MARGIN:
            candidate = raw_side
            confirmations = 0
            reversal_confirmations = 0
            active = "NONE"
            lifecycle = f"WATCH_{raw_side}"
            reason = (
                f"{raw_side} is below confirmation margin "
                f"{SIGNAL_CONFIRMATION_MIN_MARGIN}."
            )
        else:
            if candidate == raw_side:
                if can_count:
                    confirmations += 1
            else:
                candidate = raw_side
                confirmations = 1

            reversal_confirmations = 0

            if confirmations >= SIGNAL_CONFIRMATIONS_REQUIRED:
                active = raw_side
                lifecycle = f"CONFIRMED_{raw_side}"
                last_confirm = datetime_from_epoch(now)
                reason = (
                    f"{raw_side} confirmed after "
                    f"{confirmations} spaced readings."
                )
            else:
                active = "NONE"
                lifecycle = f"WATCH_{raw_side}"
                reason = (
                    f"Waiting for "
                    f"{SIGNAL_CONFIRMATIONS_REQUIRED - confirmations} "
                    f"more confirmation cycle(s)."
                )

    else:
        # --------------------------------------------------------------
        # Revalidate currently active side.
        # --------------------------------------------------------------
        active_evidence_strong = (
            (active == "CALL" and margin >= SIGNAL_HOLD_MIN_MARGIN)
            or
            (active == "PUT" and margin <= -SIGNAL_HOLD_MIN_MARGIN)
        )

        # Same side but weak evidence => RELEASE, not HOLD.
        if raw_side == active:
            if not active_evidence_strong:
                active = "NONE"
                candidate = raw_side
                confirmations = 0
                reversal_confirmations = 0
                lifecycle = f"WATCH_{raw_side}"
                reason = (
                    f"Active {raw_side} released because its supporting "
                    f"margin is weak ({margin:.1f}); fresh confirmation required."
                )
            else:
                candidate = raw_side
                confirmations = max(confirmations, SIGNAL_CONFIRMATIONS_REQUIRED)
                reversal_confirmations = 0
                lifecycle = f"HOLD_{active}"
                reason = (
                    f"{active} remains confirmed with supporting "
                    f"margin {margin:.1f}."
                )

        else:
            # ----------------------------------------------------------
            # Opposite direction.
            # ----------------------------------------------------------
            opposite_ok = (
                (raw_side == "CALL" and margin >= SIGNAL_REVERSAL_MIN_MARGIN)
                or
                (raw_side == "PUT" and margin <= -SIGNAL_REVERSAL_MIN_MARGIN)
            )

            # Active weak + opposite weak => release stale active signal.
            if not active_evidence_strong and not opposite_ok:
                released_side = active
                active = "NONE"
                candidate = raw_side
                confirmations = 0
                reversal_confirmations = 0
                lifecycle = f"WATCH_{raw_side}"
                reason = (
                    f"Active {released_side} released: its supporting margin "
                    f"is weak ({margin:.1f}) and opposite {raw_side} is not "
                    f"strong enough for reversal."
                )

            # Active still supported; opposite too weak => HOLD active.
            elif not opposite_ok:
                candidate = raw_side
                reversal_confirmations = 0
                lifecycle = f"HOLD_{active}"
                reason = (
                    f"Opposite {raw_side} reading is too weak for reversal; "
                    f"{active} remains supported."
                )

            # Opposite is strong enough: existing 2-confirmation reversal.
            else:
                if candidate == raw_side:
                    if can_count:
                        reversal_confirmations += 1
                else:
                    candidate = raw_side
                    reversal_confirmations = 1

                if reversal_confirmations >= SIGNAL_REVERSAL_CONFIRMATIONS_REQUIRED:
                    active = raw_side
                    confirmations = SIGNAL_CONFIRMATIONS_REQUIRED
                    lifecycle = f"CONFIRMED_{raw_side}"
                    last_confirm = datetime_from_epoch(now)
                    reason = (
                        f"Reversal to {raw_side} confirmed after "
                        f"{reversal_confirmations} spaced readings."
                    )
                else:
                    lifecycle = f"HOLD_{active}_REVERSAL_WATCH_{raw_side}"
                    reason = (
                        f"Possible {raw_side} reversal; holding {active} "
                        f"until confirmation."
                    )

    # CONFIRMATION_ANCHOR_FIX_20260923
    # last_evaluation_at is the anchor for the 45-second confirmation
    # interval. Do not advance it on duplicate/too-early directional
    # requests, otherwise browser refreshes can postpone confirmation
    # indefinitely.
    next_last_evaluation = last_eval
    if (
        can_count
        or lifecycle == "WAIT"
        or raw_side not in ("CALL", "PUT")
        or hard_gated
    ):
        next_last_evaluation = datetime_from_epoch(now)

    state.update({
        "active_side": active,
        "candidate_side": candidate,
        "confirmations": confirmations,
        "reversal_confirmations": reversal_confirmations,
        "lifecycle": lifecycle,
        "last_confirmation_at": last_confirm,
        "last_evaluation_at": next_last_evaluation,
        "margin": margin,
    })

    out = dict(decision)
    out["signal_lifecycle"] = lifecycle
    out["signal_candidate"] = candidate
    out["signal_confirmations"] = confirmations
    out["signal_reversal_confirmations"] = reversal_confirmations
    out["signal_active_side"] = active
    out["signal_lifecycle_reason"] = reason

    if lifecycle.startswith(("CONFIRMED_", "HOLD_")) and active in ("CALL", "PUT"):
        out["preferred_side"] = active
    else:
        # WATCH and WAIT are not active directional signals.
        out["preferred_side"] = "NONE"

    return out, state


def datetime_from_epoch(value: float):
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)

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


def _max_dampened_score_for_regime(regime: str) -> float:
    """Theoretical maximum score after regime weighting and bucket dampening."""
    mult = REGIME_BUCKET_MULTIPLIERS.get(regime, {})

    per_bucket: Dict[str, List[float]] = {}
    for name, weight in WEIGHTS.items():
        if weight <= 0:
            continue

        bucket = INDICATOR_BUCKET.get(name, name)
        regime_weight = weight * mult.get(bucket, 1.0)
        per_bucket.setdefault(bucket, []).append(regime_weight)

    total = 0.0
    for weights in per_bucket.values():
        weights.sort(reverse=True)
        for i, w in enumerate(weights):
            factor = (
                BUCKET_DIMINISHING[i]
                if i < len(BUCKET_DIMINISHING)
                else BUCKET_DIMINISHING[-1]
            )
            total += w * factor

    return total


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
    """OI buildup classification at max-CE-OI strike.

    Max OI alone ≠ Call writing — it could be long positioning, old
    positions, or hedging.  We need:
      • Short Call Buildup (bearish/resistance): CE OI ↑ + CE price weak/↓
        → confirmed call writing, acts as overhead resistance
      • Long Call Buildup (bullish): CE OI ↑ + CE price ↑
        → call buying, demand at that strike, supportive
      • Unwinding (neutral→bullish): CE OI ↓ — short covering, bearish
        pressure easing
    Strike distance from spot is a secondary filter only.
    """
    ce_top_oi   = df_summary.get("ce_max_oi_strike", 0)
    spot        = df_summary.get("spot", 0)
    # oi_change_tracked passes per-side OI change data when available
    ce_oi_chg   = df_summary.get("ce_oi_chg_atm", None)   # positive = buildup, negative = unwinding
    ce_price_chg = df_summary.get("ce_ltp_chg_atm", None)  # positive = price rose, negative = fell

    if not (ce_top_oi and spot):
        return "neutral", 0, "Call OI data அல்ல"

    dist = ce_top_oi - spot

    # If we have OI change + price direction, classify properly
    if ce_oi_chg is not None and ce_price_chg is not None:
        oi_building = ce_oi_chg > 0
        price_falling = ce_price_chg < 0
        price_rising  = ce_price_chg > 0
        if oi_building and price_falling and 0 < dist < spot * 0.025:
            return "bear", WEIGHTS["call_writing"], f"Call writing confirmed at {ce_top_oi:.0f} — OI↑ Price↓ — overhead resistance"
        if oi_building and price_rising:
            return "bull", int(WEIGHTS["call_writing"] * 0.6), f"Call buying at {ce_top_oi:.0f} — OI↑ Price↑ — bullish demand"
        if not oi_building:  # unwinding
            return "bull", int(WEIGHTS["call_writing"] * 0.4), f"Call unwinding at {ce_top_oi:.0f} — OI↓ — bearish pressure easing"
        # OI up but price ambiguous — proximity-based fallback
        if 0 < dist < spot * 0.02:
            return "bear", int(WEIGHTS["call_writing"] * 0.5), f"Call OI buildup near {ce_top_oi:.0f} (price direction unclear)"
        return "neutral", 0, f"Call OI at {ce_top_oi:.0f} — direction unclear"

    # Fallback: proximity only (no OI-change data) — reduced weight since less certain
    if 0 < dist < spot * 0.02:
        return "bear", int(WEIGHTS["call_writing"] * 0.5), f"Max Call OI near {ce_top_oi:.0f} — possible resistance (OI change unavailable)"
    elif dist > spot * 0.03:
        return "bull", int(WEIGHTS["call_writing"] * 0.3), f"Max Call OI far OTM at {ce_top_oi:.0f} — less resistance"
    return "neutral", 0, "Call OI pattern neutral"


def _score_put_writing(df_summary: Dict) -> Tuple[str, int, str]:
    """OI buildup classification at max-PE-OI strike.

    Same principle as call writing — max OI alone ≠ put writing.
      • Short Put Buildup (bullish/support): PE OI ↑ + PE price weak/↓
        → confirmed put writing, acts as downside support
      • Long Put Buildup (bearish): PE OI ↑ + PE price ↑
        → put buying, hedging/bearish demand
      • Unwinding: PE OI ↓ — put short covering, bullish pressure easing
    """
    pe_top_oi    = df_summary.get("pe_max_oi_strike", 0)
    spot         = df_summary.get("spot", 0)
    pe_oi_chg    = df_summary.get("pe_oi_chg_atm", None)
    pe_price_chg = df_summary.get("pe_ltp_chg_atm", None)

    if not (pe_top_oi and spot):
        return "neutral", 0, "Put OI data அல்ல"

    dist = spot - pe_top_oi

    if pe_oi_chg is not None and pe_price_chg is not None:
        oi_building  = pe_oi_chg > 0
        price_falling = pe_price_chg < 0
        price_rising  = pe_price_chg > 0
        if oi_building and price_falling and 0 < dist < spot * 0.025:
            return "bull", WEIGHTS["put_writing"], f"Put writing confirmed at {pe_top_oi:.0f} — OI↑ Price↓ — strong support"
        if oi_building and price_rising:
            return "bear", int(WEIGHTS["put_writing"] * 0.6), f"Put buying at {pe_top_oi:.0f} — OI↑ Price↑ — bearish demand"
        if not oi_building:
            return "bear", int(WEIGHTS["put_writing"] * 0.4), f"Put unwinding at {pe_top_oi:.0f} — OI↓ — bullish pressure easing"
        if 0 < dist < spot * 0.02:
            return "bull", int(WEIGHTS["put_writing"] * 0.5), f"Put OI buildup near {pe_top_oi:.0f} (price direction unclear)"
        return "neutral", 0, f"Put OI at {pe_top_oi:.0f} — direction unclear"

    # Fallback: proximity only
    if 0 < dist < spot * 0.02:
        return "bull", int(WEIGHTS["put_writing"] * 0.5), f"Max Put OI near {pe_top_oi:.0f} — possible support (OI change unavailable)"
    elif dist > spot * 0.03:
        return "bear", int(WEIGHTS["put_writing"] * 0.3), f"Max Put OI far OTM at {pe_top_oi:.0f} — weak support"
    return "neutral", 0, "Put OI pattern neutral"


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


def _score_volume(volume_spike: bool, volume_ratio: float, price_up: Optional[bool]) -> Tuple[str, int, str]:
    """Volume alone has no direction — a spike on a DOWN move is bearish
    (heavy selling), not bullish. price_up (spot vs VWAP, same reference
    _score_vwap uses) tells us which side the spike actually supported.
    price_up=None (VWAP unavailable) means we can't attribute a direction,
    so it scores neutral/0 like any other missing-data case in this file."""
    if not volume_spike or price_up is None:
        reason = "Volume normal" if not volume_spike else "Volume spike but direction unavailable (VWAP missing)"
        return "neutral", 0, reason
    direction = "bull" if price_up else "bear"
    verb = "strong buying" if price_up else "strong selling"
    if volume_ratio > 1.5:
        return direction, WEIGHTS["volume_spike"], f"Volume spike {volume_ratio:.1f}x avg, price {'up' if price_up else 'down'} — {verb} interest"
    return direction, int(WEIGHTS["volume_spike"] * 0.5), f"Volume slightly elevated {volume_ratio:.1f}x, price {'up' if price_up else 'down'}"


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
        if high_vol:
            return (
                "Long Straddle / Strangle",
                f"Weak/uncertain direction (margin {margin:+.0f}, ADX {adx:.1f} < 20) "
                f"ஆனா high volatility (VIX {vix:.1f}, ATR {atr_pct:.1f}%) — பெரிய move "
                f"எதிர்பார்க்கலாம் ஆனா {opt} lean மட்டும் நம்பி conviction போட போதாது. "
                f"Straddle/Strangle எந்த side move ஆனாலும் capture பண்ணும்."
            )
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
def _mtf_live_valid(multi_tf: Dict) -> bool:
    """
    Return True only when all three required MTF frames are real AND fresh.

    SAFETY FIX 2026-09-11:
    data_source="angel_one_intraday" by itself does not prove freshness.
    A stale broker response must never be used as live directional evidence.
    """
    return all(
        (
            (multi_tf.get(tf) or {}).get("data_source")
            in ("angel_one_intraday", "zerodha_intraday")
            and bool((multi_tf.get(tf) or {}).get("fresh", False))
        )
        for tf in ("5min", "15min", "1hr")
    )


def _data_availability(market_data: Dict, tech_src: str) -> Dict[str, bool]:
    # PCR/OI-change/max-pain/writing patterns all derive from the same
    # option-chain fetch — pcr<=0 is that fetch's own "empty/unavailable"
    # sentinel (see _score_pcr fix above), so it's a more direct signal
    # than the oi_summary truthiness check used before.
    chain_available = market_data.get("pcr", 0) > 0
    # SAFETY FIX 2026-09-11:
    # Daily historical close data is useful for context, but it must NOT be
    # counted as LIVE intraday technical data for directional-signal safety.
    #
    # A directional CALL/PUT decision requires all three real intraday
    # timeframes produced by MarketAnalyzer:
    #   5min + 15min + 1hr
    #
    # This prevents a situation where:
    #   technical_source = historical_daily_close
    #   MTF = unavailable
    # yet the UI incorrectly reports data_completeness = 100%.
    multi_tf = market_data.get("multi_timeframe") or {}
    # SAFETY FIX 2026-09-11:
    # Require both real broker source AND fresh candle timestamps.
    mtf_live_valid = _mtf_live_valid(multi_tf)

    # The primary technical frame must also be the real 5-minute OHLC frame.
    live_intraday_tech = (
        tech_src == "intraday_5min_ohlc"
        and mtf_live_valid
    )

    return {
        "pcr": chain_available, "oi_change": chain_available, "max_pain": chain_available,
        "call_writing": chain_available, "put_writing": chain_available,
        "futures_premium": market_data.get("futures_premium_status") == "live",

        # SAFETY FIX:
        # These technical indicators are considered directionally available
        # only when the real intraday MTF pipeline is complete.
        "vwap": live_intraday_tech, "ema20": live_intraday_tech,
        "ema50": live_intraday_tech, "rsi": live_intraday_tech,
        "macd": live_intraday_tech, "adx": live_intraday_tech,
        "supertrend": live_intraday_tech, "volume_spike": live_intraday_tech,

        "global_market": market_data.get("global_status") == "live",
        "gift_nifty": market_data.get("gift_status") == "live",
        "fii": market_data.get("fii_status") == "live",
        "dii": market_data.get("dii_status") == "live",

        # ATR has zero confidence weight, but keeping its availability tied
        # to live intraday data makes the displayed completeness truthful.
        "atr_risk": live_intraday_tech,

        # India VIX is independently available and has zero confidence weight.
        "india_vix": True,
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
    _vwap_val = technicals.get("vwap", 0)
    _price_up = (spot > _vwap_val) if _vwap_val > 0 else None
    record("volume_spike", _score_volume(technicals.get("volume_spike", False), technicals.get("volume_ratio", 1.0), _price_up))
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

    # SESSION_CONTEXT_STAGE2A_20260924
    # Session-state is an observation/context layer. It must NOT add/remove
    # score points or alter gates/lifecycle in Stage 2A.
    session_state = market_data.get("session_state") or {}
    session_phase = str(session_state.get("phase", "UNKNOWN")).upper()
    session_day_type = str(session_state.get("day_type", "UNKNOWN")).upper()
    session_volatility = str(
        session_state.get("volatility_state", "UNKNOWN")
    ).upper()
    session_transition = str(
        session_state.get("transition_state", "NONE")
    ).upper()
    session_vwap = str(
        session_state.get("vwap_relation", "UNAVAILABLE")
    ).upper()
    session_confidence = session_state.get("state_confidence", 0)

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
    hard_gated = False
    if not spot_data.get("market_open", True):
        market_bias = "Sideways"
        preferred_side = "NONE"
        hard_gated = True

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
        hard_gated = True

    # ── Critical Option-Chain Data Gate ──────────────────────────────────
    # Option chain data இல்லாமல் CALL/PUT signal கொடுப்பது தவறான design.
    # Technical indicators மட்டும் bullish ஆனாலும் option chain confirm
    # செய்யாமல் directional signal block ஆக வேண்டும்.
    #
    # chain_available = PCR > 0 (option chain successfully fetched).
    # tech_available  = not placeholder (real OHLC data, not fallback).
    #
    # CALL/PUT வர minimum requirements:
    #   1. Option chain data MUST be available (PCR > 0)
    #   2. Technical data MUST be real (not placeholder)
    #   3. Spot must be valid
    # இல்லையெனில் → WAIT (preferred_side = "NONE")
    chain_available = market_data.get("pcr", 0) > 0

    # Directional CALL/PUT signals require real intraday confirmation.
    # Daily historical technicals remain context only.
    multi_tf = market_data.get("multi_timeframe") or {}
    mtf_available = all(
        (multi_tf.get(tf) or {}).get("data_source")
        in ("angel_one_intraday", "zerodha_intraday")
        for tf in ("5min", "15min", "1hr")
    )

    tech_available = (
        market_data.get("technical_data_source") == "intraday_5min_ohlc"
        and mtf_available
    )
    spot_valid = spot > 0

    # SAFETY FIX 2026-09-11:
    # Directional CALL/PUT analysis must be blocked whenever the complete
    # real intraday MTF pipeline is unavailable.
    #
    # Required real broker-derived frames:
    #   5min + 15min + 1hr
    #
    # Daily historical data can still be shown as CONTEXT, but it must not
    # be treated as a substitute for live intraday directional evidence.
    #
    # IMPORTANT:
    # This is intentionally checked BEFORE the preferred_side branch below.
    # Therefore even a neutral margin such as -2 cannot hide the fact that
    # the live directional data pipeline is unavailable.
    multi_tf = market_data.get("multi_timeframe") or {}

    # SAFETY FIX 2026-09-11:
    # Directional analysis requires complete REAL + FRESH MTF data.
    # A stale intraday response is treated exactly like unavailable data.
    mtf_live_valid = _mtf_live_valid(multi_tf)

    # P0 OBSERVE-ONLY MTF LOGGING
    # Diagnostics only: do not change the decision, score, gate, or output.
    _mtf_observation = {}
    for _tf in ("5min", "15min", "1hr"):
        _frame = multi_tf.get(_tf) or {}
        _mtf_observation[_tf] = {
            "source": _frame.get("data_source"),
            "fresh": bool(_frame.get("fresh", False)),
            "age_min": _frame.get("freshness_minutes"),
            "timestamp": _frame.get("last_timestamp"),
            "reason": _frame.get("freshness_reason"),
            "trend": _frame.get("trend"),
        }

    logger.info(
        "MTF_OBSERVATION symbol=%s snapshot=%s technical_source=%s "
        "live_valid=%s 5min=%s 15min=%s 1hr=%s",
        market_data.get("symbol", "UNKNOWN"),
        market_data.get("timestamp"),
        market_data.get("technical_data_source"),
        mtf_live_valid,
        _mtf_observation["5min"],
        _mtf_observation["15min"],
        _mtf_observation["1hr"],
    )

    live_intraday_tech = (
        market_data.get("technical_data_source") == "intraday_5min_ohlc"
        and mtf_live_valid
    )

    if not spot_valid:
        preferred_side = "NONE"
        market_bias = "Sideways"
        risk = "High"
        hard_gated = True
        reasons.append("🚫 Spot price invalid")
        logger.warning("Decision hard-gated: invalid spot price=%s", spot)

    elif not live_intraday_tech:
        # SAFETY FIX 2026-09-11:
        # No complete real intraday MTF data = no directional signal.
        # Keep the market analysis visible, but force the decision into WAIT.
        preferred_side = "NONE"
        risk = "High"
        hard_gated = True
        market_bias = "Sideways"

        # SAFETY FIX 2026-09-11:
        # Separate "not real" from "real but stale" for diagnostics.
        missing_frames = [
            tf for tf in ("5min", "15min", "1hr")
            if (multi_tf.get(tf) or {}).get("data_source")
            not in ("angel_one_intraday", "zerodha_intraday")
        ]

        stale_frames = [
            tf for tf in ("5min", "15min", "1hr")
            if (
                (multi_tf.get(tf) or {}).get("data_source")
                in ("angel_one_intraday", "zerodha_intraday")
                and not bool((multi_tf.get(tf) or {}).get("fresh", False))
            )
        ]

        reasons.append(
            "🚫 Live intraday technical data unavailable — "
            "5m + 15m + 1h confirmation required"
        )

        if missing_frames:
            reasons.append(
                "🚫 Missing real MTF frame(s): "
                + ", ".join(missing_frames)
            )

        if stale_frames:
            reasons.append(
                "🚫 Stale MTF frame(s): "
                + ", ".join(stale_frames)
            )

            for tf in stale_frames:
                frame = multi_tf.get(tf) or {}
                reasons.append(
                    f"🚫 {tf} freshness: "
                    f"{frame.get('freshness_reason', 'stale')}"
                )

        logger.warning(
            "Decision hard-gated: live intraday MTF unavailable "
            "(technical_source=%s, missing=%s, stale=%s)",
            market_data.get("technical_data_source"),
            ",".join(missing_frames) if missing_frames else "none",
            ",".join(stale_frames) if stale_frames else "none",
        )

        logger.warning(
            "MTF gate diagnostics: "
            "5min(src=%s,fresh=%s,age=%s,reason=%s,ts=%s) "
            "15min(src=%s,fresh=%s,age=%s,reason=%s,ts=%s) "
            "1hr(src=%s,fresh=%s,age=%s,reason=%s,ts=%s)",
            (multi_tf.get("5min") or {}).get("data_source"),
            (multi_tf.get("5min") or {}).get("fresh"),
            (multi_tf.get("5min") or {}).get("freshness_minutes"),
            (multi_tf.get("5min") or {}).get("freshness_reason"),
            (multi_tf.get("5min") or {}).get("last_timestamp"),
            (multi_tf.get("15min") or {}).get("data_source"),
            (multi_tf.get("15min") or {}).get("fresh"),
            (multi_tf.get("15min") or {}).get("freshness_minutes"),
            (multi_tf.get("15min") or {}).get("freshness_reason"),
            (multi_tf.get("15min") or {}).get("last_timestamp"),
            (multi_tf.get("1hr") or {}).get("data_source"),
            (multi_tf.get("1hr") or {}).get("fresh"),
            (multi_tf.get("1hr") or {}).get("freshness_minutes"),
            (multi_tf.get("1hr") or {}).get("freshness_reason"),
            (multi_tf.get("1hr") or {}).get("last_timestamp"),
        )

    elif preferred_side in ("CALL", "PUT"):
        # RANGE structural guard:
        # STEP 2K-AS historical counterfactual:
        # RANGE + CALL + 15m DOWN + 1h DOWN
        # removed 127 signals: UP=6, DOWN=93, FLAT=28.
        # Evaluated accuracy improved by +20.45pp.
        # TIMEFRAME TREND SOURCE
        # Prefer the normalized timeframe_trend contract when present.
        # Otherwise derive it directly from MarketAnalyzer's authoritative
        # multi_timeframe payload. The API route creates timeframe_trend
        # only after the decision engine has already run.
        timeframe_trend = market_data.get("timeframe_trend") or {}

        if not timeframe_trend:
            multi_tf = market_data.get("multi_timeframe") or {}
            timeframe_trend = {
                "5min": multi_tf.get("5min", {}).get("trend", "unavailable"),
                "15min": multi_tf.get("15min", {}).get("trend", "unavailable"),
                "1hr": multi_tf.get("1hr", {}).get("trend", "unavailable"),
            }

        trend_15m = str(
            timeframe_trend.get("15min", "unavailable")
        ).lower()
        trend_1h = str(
            timeframe_trend.get("1hr", "unavailable")
        ).lower()

        structural_range_call_block = (
            regime == "RANGE"
            and preferred_side == "CALL"
            and trend_15m == "down"
            and trend_1h == "down"
        )

        if structural_range_call_block:
            preferred_side = "NONE"
            risk = "High"
            hard_gated = True
            reasons.append(
                "🚫 RANGE CALL blocked: 15m and 1h trends are both DOWN"
            )
            logger.info(
                "RANGE CALL blocked by structural guard: "
                "15m=%s 1h=%s margin=%.1f",
                trend_15m,
                trend_1h,
                margin,
            )

        # Existing weak-margin guard remains as a second safeguard.
        if (
            preferred_side == "CALL"
            and regime == "RANGE"
            and 10 <= margin < 15
        ):
            preferred_side = "NONE"
            risk = "High"
            hard_gated = True
            reasons.append(
                f"🚫 RANGE CALL blocked: margin +{margin:.1f} "
                "is in the weak +10..+14 confirmation zone"
            )
            logger.info(
                "RANGE CALL blocked by quick guard: margin=%.1f",
                margin,
            )

        blocking_reasons = []
        if not chain_available:
            blocking_reasons.append("Option chain data unavailable (PCR=0) — CALL/PUT signal blocked")
        if not tech_available:
            blocking_reasons.append("Technical data is placeholder — real OHLC unavailable")

        # SAFETY FIX 2026-09-11:
        # The 5-minute timeframe is useful for short-term timing, but it
        # must not override a clear higher-timeframe structure by itself.
        #
        # Example:
        #   5m  = UP
        #   15m = DOWN
        #   1h  = DOWN
        #
        # A fresh CALL in this structure is conflicting with both higher
        # timeframes. Therefore block the fresh CALL and force WAIT.
        #
        # Symmetric protection:
        #   5m  = DOWN
        #   15m = UP
        #   1h  = UP
        #
        # In that case a fresh PUT is blocked.
        #
        # IMPORTANT:
        # This is an ENTRY SAFETY GATE only.
        # It does NOT change market_regime. The regime classifier can still
        # report TREND_UP/TREND_DOWN based on its authoritative technical
        # context. We only prevent a conflicting fresh directional signal.
        mtf = market_data.get("multi_timeframe") or {}

        mtf_5m = str(
            (mtf.get("5min") or {}).get("trend", "unavailable")
        ).lower()
        mtf_15m = str(
            (mtf.get("15min") or {}).get("trend", "unavailable")
        ).lower()
        mtf_1h = str(
            (mtf.get("1hr") or {}).get("trend", "unavailable")
        ).lower()

        # Only apply the conflict gate when ALL three timeframe streams
        # are real broker-derived intraday data. Missing data is handled by
        # the existing technical-data safety gate above.
        # SAFETY FIX 2026-09-11:
        # MTF conflict detection must also require fresh candles.
        # Otherwise stale 15m/1h data could incorrectly block a new signal.
        mtf_live_valid = _mtf_live_valid(mtf)

        mtf_conflict_call = (
            mtf_live_valid
            and mtf_5m == "up"
            and mtf_15m == "down"
            and mtf_1h == "down"
            and preferred_side == "CALL"
        )

        mtf_conflict_put = (
            mtf_live_valid
            and mtf_5m == "down"
            and mtf_15m == "up"
            and mtf_1h == "up"
            and preferred_side == "PUT"
        )

        if mtf_conflict_call:
            preferred_side = "NONE"
            risk = "High"
            hard_gated = True
            reasons.append(
                "🚫 MTF conflict: 5m UP but 15m and 1h DOWN — fresh CALL blocked"
            )
            logger.warning(
                "MTF conflict safety gate: blocking fresh CALL "
                "(5m=UP, 15m=DOWN, 1h=DOWN)"
            )

        elif mtf_conflict_put:
            preferred_side = "NONE"
            risk = "High"
            hard_gated = True
            reasons.append(
                "🚫 MTF conflict: 5m DOWN but 15m and 1h UP — fresh PUT blocked"
            )
            logger.warning(
                "MTF conflict safety gate: blocking fresh PUT "
                "(5m=DOWN, 15m=UP, 1h=UP)"
            )

        if blocking_reasons:
            preferred_side = "NONE"
            risk = "High"
            hard_gated = True
            if market_bias not in ("Sideways",):
                market_bias = "Sideways"
            for br in blocking_reasons:
                reasons.append(f"🚫 {br}")
            logger.warning(
                "CALL/PUT signal blocked due to data gate: %s",
                "; ".join(blocking_reasons),
            )

    # ── Signal hysteresis — see module docstring above for why this exists.
    # Hard safety gates (market closed / NO_TRADE regime / data unavailable)
    # always win and are never overridden by a held-over signal. Outside
    # those, a signal that would otherwise flip to NONE purely because
    # margin dipped below the ±10 entry threshold (but not past the softer
    # ±HYSTERESIS_EXIT_MARGIN) keeps showing the previous side instead.
    symbol_key = market_data.get("symbol", "NIFTY")
    prev_state = _signal_state.get(symbol_key)

    # BUG FIX 2026-09-11:
    # Hysteresis may continue ONLY an already-confirmed active direction.
    # A WATCH candidate must never be used as the previous signal.
    prev_active_side = (
        prev_state.get("active_side", "NONE")
        if prev_state
        else "NONE"
    )

    if (
        not hard_gated
        and preferred_side == "NONE"
        and prev_state
        and prev_active_side in ("CALL", "PUT")
    ):
        prev_side = prev_active_side
        if prev_side == "CALL" and margin > HYSTERESIS_EXIT_MARGIN:
            preferred_side = "CALL"
            reasons.append(
                f"↔️ Holding previous CALL signal — margin {margin} weakened but hasn't "
                f"crossed the exit band (±{HYSTERESIS_EXIT_MARGIN}); avoids flip-flopping on noise"
            )
        elif prev_side == "PUT" and margin < -HYSTERESIS_EXIT_MARGIN:
            preferred_side = "PUT"
            reasons.append(
                f"↔️ Holding previous PUT signal — margin {margin} weakened but hasn't "
                f"crossed the exit band (±{HYSTERESIS_EXIT_MARGIN}); avoids flip-flopping on noise"
            )
    # Convert the hysteresis result into a stable signal lifecycle. Raw
    # CALL/PUT readings need consecutive confirmation; active signals are
    # held through temporary noise and reversals need confirmation too.
    # Preserve the scoring/gate result before lifecycle can convert WATCH
    # into preferred_side=NONE. Persistent lifecycle needs this raw candidate
    # to accumulate confirmations across requests.
    raw_preferred_side = preferred_side
    raw_lifecycle_side = raw_preferred_side
    lifecycle_side, lifecycle = _apply_signal_lifecycle(
        symbol_key, raw_lifecycle_side, margin, hard_gated
    )
    if lifecycle_side in ("CALL", "PUT") and lifecycle["state"].startswith(("CONFIRMED_", "HOLD_")):
        preferred_side = lifecycle_side
    elif lifecycle["state"].startswith("WATCH_"):
        # BUG FIX 2026-09-11:
        # WATCH is a candidate-only state, not an actionable directional signal.
        # Keep the candidate visible through signal_candidate/lifecycle,
        # but require full confirmation before exposing CALL/PUT.
        preferred_side = "NONE"
    elif hard_gated:
        preferred_side = "NONE"

    _signal_state[symbol_key] = {
        "side": "NONE" if hard_gated else preferred_side,
        "margin": margin,
        "candidate_side": lifecycle.get("candidate_side", "NONE"),
        "confirmations": lifecycle.get("confirmations", 0),
        "reversal_confirmations": lifecycle.get("reversal_confirmations", 0),
        "active_side": lifecycle.get("active_side", "NONE"),
        "lifecycle": lifecycle.get("state", "WAIT"),
        "last_confirmation_ts": lifecycle.get("last_confirmation_ts", 0),
        "ts": time.time(),
    }

    # ── Confidence (rule-based, not random) ──────────────────────────────
    # Maximum possible = the dampened theoretical max (see
    # _max_dampened_score / MAX_DAMPENED_SCORE above), not a raw weight sum —
    # since bull/bear are now bucket-dampened (review #7), normalising
    # against the old undampened sum would make confidence read
    # artificially low across the board.
    max_possible = _max_dampened_score_for_regime(regime)
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

    # CLOSED_MARKET_REASON_SANITIZATION_20260922
    # The market-closed hard gate above already blocks directional signals.
    # Do not let unavailable sentinel values (ADX/VIX/ATR = 0) generate a
    # live-market strategy description such as RANGE / Iron Condor.
    if not spot_data.get("market_open", True):
        forecast = "Market Closed"
        strategy = "WAIT"
        strategy_reason = (
            "Market closed — live intraday technical data unavailable. "
            "No fresh directional strategy is generated."
        )

    # ── Scenarios + invalidation levels ───────────────────────────────────
    # "What would prove this view wrong" is as important as the view itself
    # — a bias without an invalidation level isn't falsifiable. Built from
    # the same support/resistance levels already computed for this request.
    sr = market_data.get("support_resistance", {}) or {}
    scenarios = _build_scenarios(spot, sr, bullish_probability, bearish_probability, margin)

    # SESSION_CONTEXT_STAGE2A_20260924
    # Descriptive only: this tells the UI how the score sits inside today's
    # intraday session structure. No score/gate/lifecycle mutation.
    mtf5_context = str(
        ((market_data.get("multi_timeframe") or {}).get("5min") or {}).get(
            "trend", "unavailable"
        )
    ).upper()
    # SESSION_CONTEXT_NORMALIZE_20260924
    # Keep timeframe labels in a stable uppercase contract.
    mtf15_context = str(
        ((market_data.get("multi_timeframe") or {}).get("15min") or {}).get(
            "trend", "unavailable"
        )
    ).upper()

    if (
        session_phase == "MIDDAY"
        and session_volatility == "COMPRESSION"
    ):
        session_environment = "MIDDAY_COMPRESSION"
    elif session_transition == "REVERSAL_WATCH":
        session_environment = "REVERSAL_WATCH"
    elif session_transition == "TRANSITION":
        session_environment = "TRANSITION"
    elif session_phase in {"AFTERNOON_EXPANSION", "CLOSING"}:
        session_environment = "AFTERNOON_EXPANSION"
    else:
        session_environment = session_phase

    session_alignment = "NEUTRAL"

    if preferred_side == "CALL":
        if (
            mtf5_context == "UP"
            and mtf15_context == "UP"
            and session_vwap == "ABOVE"
        ):
            session_alignment = "ALIGNED"
        elif (
            mtf5_context == "DOWN"
            or mtf15_context == "DOWN"
            or session_vwap == "BELOW"
        ):
            session_alignment = "CONFLICTING"
    elif preferred_side == "PUT":
        if (
            mtf5_context == "DOWN"
            and mtf15_context == "DOWN"
            and session_vwap == "BELOW"
        ):
            session_alignment = "ALIGNED"
        elif (
            mtf5_context == "UP"
            or mtf15_context == "UP"
            or session_vwap == "ABOVE"
        ):
            session_alignment = "CONFLICTING"

    session_context = {
        "phase": session_phase,
        "day_type": session_day_type,
        "environment": session_environment,
        "volatility_state": session_volatility,
        "transition_state": session_transition,
        "vwap_relation": session_vwap,
        "trend_5m": mtf5_context,
        "trend_15m": mtf15_context,
        "alignment": session_alignment,
        "state_confidence": session_confidence,
        "observation_only": True,
    }

    if session_environment == "MIDDAY_COMPRESSION":
        reasons.append(
            "🕒 Session context: MIDDAY compression — "
            "directional score unchanged; breakout confirmation remains separate."
        )
    elif session_environment == "REVERSAL_WATCH":
        reasons.append(
            "🔄 Session context: REVERSAL_WATCH — "
            "score unchanged; current direction is being monitored against the morning structure."
        )
    elif session_environment == "TRANSITION":
        reasons.append(
            "🔄 Session context: TRANSITION — "
            "score unchanged; intraday structure is changing."
        )

    # ── Recommended strike (ATM by default; ATM+1 when direction bias is
    # weak/uncertain — cheaper premium for a less-confident read) ────────
    if preferred_side == "NONE":
        recommended_strike = "NONE"
    elif adx_val >= 25 and abs(margin) >= 20:
        recommended_strike = "ATM"
    else:
        recommended_strike = "ATM+1"

    return {
        "market_open":           spot_data.get("market_open", True),
        "market_bias":           market_bias,
        "bullish_probability":   bullish_probability,
        "bearish_probability":   bearish_probability,
        "preferred_side":        preferred_side,
        "raw_preferred_side":     raw_preferred_side,
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
        "session_context":       session_context,
        "market_regime_reasons": regime_info.get("reasons", []),
        "market_regime_no_trade": regime_info.get("no_trade", False),
        "market_regime_no_trade_reason": regime_info.get("no_trade_reason", ""),
        "margin":                margin,
        "signal_lifecycle":      lifecycle.get("state", "WAIT"),
        "signal_candidate":      lifecycle.get("candidate_side", "NONE"),
        "signal_confirmations":  lifecycle.get("confirmations", 0),
        "signal_reversal_confirmations": lifecycle.get("reversal_confirmations", 0),
        "signal_active_side":    lifecycle.get("active_side", "NONE"),
        "signal_lifecycle_reason": lifecycle.get("reason", ""),
        "hard_gated":            hard_gated,
        "reasons":               reasons,
        "strategy":              strategy,
        "strategy_reason":       strategy_reason,
        "scenarios":             scenarios,
    }
