from fastapi import APIRouter, Depends, HTTPException
from app.services.market_analyzer import MarketAnalyzer
from app.services.ai_engine import AIEngine
from app.exceptions import AIProviderError, MarketDataError
from app.api.deps import get_analyzer, get_ai_engine
from app.schemas import AIAnalysisRouteResponse, AnalysisDecisionResponse
from app.utils.helpers import safe_float
from app.utils.ai_result_cache import get_ai_analysis
from app.services.strategy_history import load_signal_state
from app.services.history_service import save_analysis_result
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


def _overlay_persisted_lifecycle(decision: dict, persisted_state: dict | None) -> dict:
    """Read-only lifecycle view for Analysis/Live/Terminal.

    IMPORTANT:
    - Never increments confirmations.
    - Never saves state.
    - Never mutates persistent lifecycle state.
    - Strategy route remains the sole owner of lifecycle progression.
    """
    out = dict(decision)
    state = persisted_state or {}

    raw_side = str(
        decision.get("raw_preferred_side",
                    decision.get("preferred_side", "NONE"))
        or "NONE"
    ).upper()

    hard_gated = bool(decision.get("hard_gated", False)) or (
        decision.get("market_open") is not True
    )

    # Current safety gate always wins over an old persisted signal.
    if hard_gated:
        out["preferred_side"] = "NONE"
        out["signal_lifecycle"] = "WAIT"
        out["signal_candidate"] = "NONE"
        out["signal_confirmations"] = 0
        out["signal_reversal_confirmations"] = 0
        out["signal_active_side"] = "NONE"
        return out

    # If the current raw engine has no directional candidate, do not expose
    # an old persisted CALL/PUT as the current analysis signal.
    if raw_side not in ("CALL", "PUT"):
        out["preferred_side"] = "NONE"
        out["signal_lifecycle"] = "WAIT"
        out["signal_candidate"] = "NONE"
        out["signal_confirmations"] = 0
        out["signal_reversal_confirmations"] = 0
        out["signal_active_side"] = "NONE"
        return out

    lifecycle = str(state.get("lifecycle", "WAIT") or "WAIT")
    candidate = str(state.get("candidate_side", "NONE") or "NONE").upper()
    active = str(state.get("active_side", "NONE") or "NONE").upper()

    # No persisted lifecycle yet: show the raw candidate as WATCH.
    if lifecycle == "WAIT" or not lifecycle:
        out["preferred_side"] = "NONE"
        out["signal_lifecycle"] = f"WATCH_{raw_side}"
        out["signal_candidate"] = raw_side
        out["signal_confirmations"] = 0
        out["signal_reversal_confirmations"] = 0
        out["signal_active_side"] = "NONE"
        return out

    out["signal_lifecycle"] = lifecycle
    out["signal_candidate"] = candidate
    out["signal_confirmations"] = int(state.get("confirmations", 0) or 0)
    out["signal_reversal_confirmations"] = int(
        state.get("reversal_confirmations", 0) or 0
    )
    out["signal_active_side"] = active

    # Only an already persisted CONFIRMED/HOLD state can expose a
    # directional preferred_side to the Analysis UI.
    if (
        lifecycle.startswith(("CONFIRMED_", "HOLD_"))
        and active in ("CALL", "PUT")
    ):
        out["preferred_side"] = active
    else:
        out["preferred_side"] = "NONE"

    return out


def _pick_recommended_option(market_data: dict, dec: dict, action: str = "") -> dict:
    """Picks ONE concrete strike (contract) matching the rule engine's
    preferred_side + recommended_strike label (ATM / ATM+1), with a
    mid-price entry estimate. Review #1: this is what lets the accuracy
    engine grade the SIGNAL AN OPTION BUYER WOULD ACTUALLY TAKE — a specific
    contract's premium — instead of only grading spot direction, which the
    review correctly points out can be "right" while the option itself
    loses money to theta/IV crush. Reuses strategy.py's own strike-picking
    (`_pick_strikes` — liquidity filter, mid-price, Greeks) as the single
    source of truth instead of a second, drifting implementation."""
    # The final machine action is the single source of truth.
    # A preferred_side/lifecycle alone must NEVER create a concrete option
    # while the final strategy/action is WAIT.
    final_action = str(action or "").strip().upper()
    action_map = {
        "BUY CE": ("CALL", "BUY CE"),
        "BUY PE": ("PUT", "BUY PE"),
        "SELL CE": ("CALL", "SELL CE"),
        "SELL PE": ("PUT", "SELL PE"),
    }

    if final_action not in action_map:
        return {
            "available": False,
            "reason": f"No actionable final strategy ({final_action or 'WAIT'}) — no option contract to track",
        }

    side, option_action = action_map[final_action]

    chain = market_data.get("option_chain") or {}
    spot = (market_data.get("spot") or {}).get("price", 0)
    if not chain.get("data") or spot <= 0:
        return {"available": False, "reason": "Option chain unavailable"}

    from app.api.routes.strategy import _pick_strikes  # shared contract selector
    from app.utils.helpers import intraday_hold_days_to_close

    atr = safe_float((market_data.get("technicals") or {}).get("atr", 0))
    picks = _pick_strikes(
        chain,
        is_call=(side == "CALL"),
        spot=spot,
        atr=atr,
        action=option_action,
        assumed_hold_days=intraday_hold_days_to_close(
            market_data.get("timestamp")
        ),
    )
    if not picks:
        return {"available": False, "reason": "No liquid strike found near ATM"}

    # recommended_strike label from decision_engine is "ATM" or "ATM+1" —
    # _pick_strikes' first ("Best Strike") pick IS the ATM+1/ATM-1 liquidity
    # pick already; fall back to it either way since it's always liquid.
    chosen = picks[0]
    return {
        "available":    True,
        "symbol":       chosen.get("symbol"),
        "strike":       chosen["strike"],
        "type":         chosen["type"],
        "expiry":       chosen["expiry"],
        # "Best Strike" / "Aggressive (OTM)" / "Conservative (ITM)" — kept so
        # the accuracy engine can grade premium outcomes PER STRIKE LABEL
        # (signal_accuracy.compute_premium_accuracy's by_moneyness), not just
        # overall — review point #39/#40 ("எந்த strike வாங்கினால் அதிக probability?").
        "label":        chosen.get("label"),
        "entry_price":  chosen["entry_price"],   # mid-price estimate — see options_greeks.mid_price
        "entry_ltp":    chosen["ltp"],
        # Preserve the complete executable option snapshot produced by
        # _pick_strikes(). This includes broker/derived IV and Greeks so
        # AnalysisResult history stores the same contract metrics shown by
        # the Strategy route instead of silently dropping them.
        "ltp":          chosen.get("ltp"),
        "bid":           chosen.get("bid"),
        "ask":           chosen.get("ask"),
        "iv":            chosen.get("iv"),
        "iv_source":     chosen.get("iv_source"),
        # Review #5: structured CALL/PUT recommendation (SL/targets/theta
        # risk alongside strike/entry), not just a bare "CALL BUY" string —
        # _pick_strikes already computes all of this for the /strategy
        # route; surfacing it here too means the SAME numbers a user would
        # see on the Strategy page are what the Accuracy engine grades
        # against, instead of two independent SL/target calculations
        # potentially drifting apart.
        "sl":           chosen.get("sl"),
        "t1":           chosen.get("t1"),
        "t2":           chosen.get("t2"),
        "t3":           chosen.get("t3"),
        "risk_reward":  chosen.get("rr"),
        "delta":        chosen.get("delta"),
        "theta_per_day": chosen.get("theta_per_day"),
        "theta_pct_of_entry": chosen.get("theta_pct_of_entry"),
        "vega":         chosen.get("vega"),
        "spread_pct":   chosen.get("spread_pct"),
        "oi":           chosen.get("oi"),
        "volume":       chosen.get("volume"),
        "price_source": chosen.get("price_source"),
        "live_ws":      chosen.get("live_ws", False),
        "live_ws_source": chosen.get("live_ws_source"),
        "liquidity_note": chosen.get("note"),
    }



async def _apply_entry_risk_authority(result: dict, market_data: dict) -> dict:
    """Apply the same V2 risk authority used by the Strategy route.

    The resolver supplies a candidate, not risk approval. Only a validated
    BUY CE/BUY PE assessment can authorize a fresh Analysis entry.
    """
    from app.services.entry_risk_authority import (
        build_entry_risk_authority,
        is_entry_risk_approved,
    )
    from app.services.entry_risk_service import assess_entry_risk_authority

    action = str(
        result.get("signal_action") or result.get("best_strategy") or "WAIT"
    ).strip().upper()
    lifecycle = str(result.get("signal_lifecycle") or "WAIT").strip().upper()

    gate = result.get("entry_gate") or result.get("v3_gate") or {}
    gate = dict(gate) if isinstance(gate, dict) else {}

    regime = result.get("market_regime")
    regime_blocked = bool(result.get("market_regime_no_trade")) or bool(
        isinstance(regime, dict)
        and (
            regime.get("hard_no_trade") is True
            or regime.get("no_trade") is True
        )
    )

    candidate_is_fresh = (
        action in ("BUY CE", "BUY PE", "SELL CE", "SELL PE")
        and lifecycle in ("CONFIRMED_CALL", "CONFIRMED_PUT")
        and result.get("fresh_entry_allowed") is True
        and gate.get("allowed") is True
        and gate.get("fresh_entry_allowed") is True
        and not bool(result.get("hard_gated", False))
        and not regime_blocked
    )

    risk_assessment = None
    trade_levels = None

    if not candidate_is_fresh:
        authority = build_entry_risk_authority(action=action)
        risk_approved = False
    else:
        selected = result.get("recommended_option")
        selected_contract = (
            dict(selected)
            if isinstance(selected, dict) and selected.get("available") is True
            else None
        )

        try:
            if action in ("BUY CE", "BUY PE") and selected_contract:
                from app.services.contract_specs import resolve_lot_size
                from app.services.trade_levels import calculate_trade_levels
                from app.utils.helpers import intraday_hold_days_to_close

                spot_data = market_data.get("spot")
                spot_data = spot_data if isinstance(spot_data, dict) else {}
                spot = safe_float(spot_data.get("price"))

                chain = market_data.get("option_chain")
                chain = chain if isinstance(chain, dict) else {}

                lot_size = resolve_lot_size(
                    result.get("symbol") or market_data.get("symbol"),
                    chain,
                )

                trade_levels = calculate_trade_levels(
                    market_data,
                    "bullish" if action == "BUY CE" else "bearish",
                    spot,
                    safe_float(selected_contract.get("entry_price")),
                    lot_size=lot_size,
                    delta=selected_contract.get("delta"),
                    theta_per_day=selected_contract.get("theta_per_day"),
                    assumed_hold_days=intraday_hold_days_to_close(
                        market_data.get("timestamp")
                    ),
                    regime=(
                        regime.get("regime", "UNKNOWN")
                        if isinstance(regime, dict)
                        else str(regime or "UNKNOWN")
                    ),
                )

                selected_contract.update({
                    "entry_price": trade_levels.get("option_entry"),
                    "sl": trade_levels.get("option_sl"),
                    "expiry": (
                        selected_contract.get("expiry")
                        or chain.get("expiry")
                    ),
                })
            else:
                lot_size = None

            assessment = await assess_entry_risk_authority(
                action=action,
                symbol=result.get("symbol") or market_data.get("symbol") or "",
                market_data=market_data,
                confluence=result.get("confluence") or {},
                regime=regime if isinstance(regime, dict) else {},
                signal_strength=result.get(
                    "signal_strength", result.get("confidence", 0)
                ),
                selected_contract=selected_contract,
                trade_levels=trade_levels,
                lot_size=lot_size,
            )

            risk_assessment = assessment.get("risk_assessment")
            authority = assessment.get("entry_risk_authority")
            risk_approved = (
                assessment.get("risk_approved") is True
                and is_entry_risk_approved(authority)
                and authority.get("action") == action
            )
        except Exception as exc:
            logger.warning(
                "Analysis entry risk assessment failed for %s: %s",
                result.get("symbol"),
                exc,
            )
            authority = build_entry_risk_authority(
                action=action,
                assessment_error=exc,
                selected_contract=selected_contract,
                trade_levels=trade_levels,
                lot_size=lot_size if "lot_size" in locals() else None,
            )
            risk_approved = False

    result["metadata_version"] = 2
    result["entry_risk_authority"] = authority
    result["fresh_entry_allowed"] = bool(risk_approved)
    result["v2_risk"] = risk_assessment if risk_approved else None
    result["v2_trade_levels"] = trade_levels if risk_approved else None

    gate["entry_risk_authority"] = authority
    gate["risk_allowed"] = bool(risk_approved)
    gate["fresh_entry_allowed"] = bool(risk_approved)
    result["v3_gate"] = gate
    result["entry_gate"] = gate

    if risk_approved and isinstance(selected_contract, dict):
        selected_contract["entry_price"] = trade_levels.get("option_entry")
        selected_contract["sl"] = trade_levels.get("option_sl")
        selected_contract["t1"] = trade_levels.get("option_t1")
        selected_contract["t2"] = trade_levels.get("option_t2")
        selected_contract["t3"] = trade_levels.get("option_t3")
        result["recommended_option"] = selected_contract
    elif lifecycle in ("CONFIRMED_CALL", "CONFIRMED_PUT") and action in (
        "BUY CE", "BUY PE", "SELL CE", "SELL PE"
    ):
        result["signal_action"] = "WAIT"

    return result


def _metric_quality_fields(market_data: dict) -> dict:
    """Consistent, additive quality metadata for the analysis API."""
    md = market_data if isinstance(market_data, dict) else {}
    chain = md.get("option_chain") or {}
    chain_valid = bool(
        md.get("option_chain_valid", bool(chain.get("data")))
    )

    try:
        vix = float(md.get("vix") or 0)
    except (TypeError, ValueError):
        vix = 0.0

    vix_quality = md.get("vix_quality")
    if isinstance(vix_quality, dict):
        vix_quality = dict(vix_quality)
    else:
        vix_quality = {
            "available": vix > 0,
            "source": "legacy_unverified" if vix > 0 else "unavailable",
            "fetched_at": None,
            "observed_at": None,
            "reason": (
                "Provider metadata unavailable" if vix > 0
                else "VIX unavailable"
            ),
        }
    vix_quality["available"] = bool(
        vix_quality.get("available") and vix > 0
    )

    try:
        pcr = float(md.get("pcr") or 0)
    except (TypeError, ValueError):
        pcr = 0.0

    pcr_quality = md.get("pcr_quality")
    if isinstance(pcr_quality, dict):
        pcr_quality = dict(pcr_quality)
    else:
        pcr_quality = {
            "available": chain_valid and pcr > 0,
            "source": str(chain.get("data_source") or "unknown"),
            "fetched_at": None,
            "observed_at": None,
            "reason": (
                None if chain_valid and pcr > 0
                else "Option-chain/PCR evidence unavailable"
            ),
        }
    pcr_quality["available"] = bool(
        pcr_quality.get("available") and chain_valid and pcr > 0
    )

    return {
        "vix_quality": vix_quality,
        "pcr_quality": pcr_quality,
        "option_chain_valid": chain_valid,
    }


async def build_ai_analysis(
    symbol: str,
    analyzer: MarketAnalyzer,
    ai: AIEngine,
    expiry: str = None,
    cache_bust: str = "",
    market_data_override: dict = None,
    ai_result_override: dict = None,
    persisted_state_override: dict = None,
    ai_unavailable: bool = False,
) -> dict:
    """Builds the exact same result shape /api/analysis/ai/{symbol} returns —
    factored out so both the HTTP route AND the background history collector
    (app/services/history_collector.py) save identical, compatible rows.
    Previously this logic lived only inside the route handler, which meant
    AnalysisResult rows (needed by the Accuracy page / signal_accuracy.py)
    only got written when a human happened to have the Analysis page open —
    the background collector calls this function on its own schedule so
    history keeps accumulating even with zero browser tabs open."""
    if market_data_override is not None:
        market_data = market_data_override
    else:
        try:
            market_data = await analyzer.get_full_market_overview(
                symbol,
                expiry=expiry,
                cache_bust=cache_bust,
            )
        except MarketDataError as e:
            raise HTTPException(502, detail=f"Market data unavailable: {e}")

    try:
        if ai_unavailable:
            raise AIProviderError("AI result unavailable; use rule-engine fallback")
        if ai_result_override is not None:
            result = dict(ai_result_override)
        else:
            resolved_expiry = market_data.get("option_chain", {}).get("expiry", "")
            result = await get_ai_analysis(ai, symbol, resolved_expiry, market_data)
    except AIProviderError as e:
        # AI fail ஆனாலும் rule-engine result return செய் — analysis only,
        # no buy/sell instruction anywhere in this fallback either.
        dec = market_data.get("decision", {})

        # AI did not answer, so agreement is unknown rather than True.
        # Expose the actual broker MTF source used by the rule engine.
        multi_tf = market_data.get("multi_timeframe", {}) or {}
        real_tf_sources = ("angel_one_intraday", "zerodha_intraday")
        fallback_tf_source = next(
            (
                multi_tf.get(label, {}).get("data_source")
                for label in ("5min", "15min", "1hr")
                if multi_tf.get(label, {}).get("data_source") in real_tf_sources
            ),
            "unavailable",
        )

        result = {
            "market_bias":         dec.get("market_bias", "Sideways"),
            "bullish_probability": dec.get("bullish_probability", 50),
            "bearish_probability": dec.get("bearish_probability", 50),
            "preferred_side":      dec.get("preferred_side", "NONE"),
            "market_trend":        market_data.get("trend", "sideways"),
            "reason":              "AI unavailable. Rule-engine analysis shown.",
            "key_factors":         [r for r in dec.get("reasons", [])[:5]],
            "timeframe_trend":     {"5min": "N/A", "15min": "N/A", "1hr": "N/A"},
            "timeframe_data_source": fallback_tf_source,
            "support":             market_data.get("support_resistance", {}).get("support", []),
            "resistance":          market_data.get("support_resistance", {}).get("resistance", []),
            "risk":                dec.get("risk", "Medium"),
            "vix":                 market_data.get("vix", 0),
            **_metric_quality_fields(market_data),
            "pcr":                 market_data.get("pcr", 0),
            "bull_score":          dec.get("bull_score", 0),
            "bear_score":          dec.get("bear_score", 0),
            "margin":               dec.get(
                "margin",
                (dec.get("bull_score", 0) or 0)
                - (dec.get("bear_score", 0) or 0),
            ),
            "confidence":          dec.get("confidence", 0),
            "rule_confidence":      dec.get("confidence", 0),
            "rule_market_bias":     dec.get("market_bias", "Sideways"),
            "forecast":             dec.get("forecast", "Neutral"),
            "ai_agrees":           None,
            "_provider":           "rule_engine_only",
            "disclaimer":          "Informational analysis only — not investment advice.",
        }

    # Full reasons list + strategy/price-level detail சேர்க்க
    dec = market_data.get("decision", {})
    result["all_reasons"]        = dec.get("reasons", [])
    # AIAnalysisRouteResponse requires these fields for every response,
    # including normal AI-success responses and rule-engine fallback.
    result["rule_confidence"]    = dec.get("confidence", 0)
    result["rule_market_bias"]   = dec.get("market_bias", "Sideways")
    # Authoritative rule-engine margin; AI is only a validator/explainer.
    result["margin"]             = dec.get(
        "margin",
        (dec.get("bull_score", 0) or 0)
        - (dec.get("bear_score", 0) or 0),
    )
    # SPOT_RESPONSE_EXPOSE_FIX_20260924
    # MarketAnalyzer already has the authoritative fresh spot snapshot.
    # Expose that same object through /api/analysis/ai so the UI/API
    # does not report spot=None while the Angel WebSocket is healthy.
    result["spot"] = market_data.get("spot", {})
    result["recommended_strike"] = dec.get("recommended_strike", "NONE")
    result["signal_lifecycle"] = dec.get("signal_lifecycle", "WAIT")
    result["signal_candidate"] = dec.get("signal_candidate", "NONE")
    result["signal_confirmations"] = dec.get("signal_confirmations", 0)
    result["signal_reversal_confirmations"] = dec.get("signal_reversal_confirmations", 0)
    result["signal_active_side"] = dec.get("signal_active_side", "NONE")
    result["strategy"]           = dec.get("strategy", "")
    result["hard_gated"]           = dec.get("hard_gated", False)
    result["strategy_reason"]    = dec.get("strategy_reason", "")
    result["strategy_detail"]    = dec.get("strategy_detail")
    result["price_levels"]       = dec.get("price_levels")
    result["max_pain"]           = market_data.get("max_pain", 0)
    result["technicals"]         = market_data.get("technicals", {})
    # Use the MarketAnalyzer source-of-truth instead of allowing an AI
    # payload to leave this field as None/unknown.
    result["technical_data_source"] = market_data.get(
        "technical_data_source", "unknown"
    )
    result["oi_summary"]         = market_data.get("oi_summary", {})
    result["option_volume"]      = market_data.get("option_volume", {})
    result["expiry"]             = market_data.get("option_chain", {}).get("expiry", "")
    result["all_expiries"]       = market_data.get("option_chain", {}).get("all_expiries", [])

    # AI can hallucinate a 5min/15min/1hr trend from daily-only data — when
    # this app actually HAS real intraday candles (Angel One configured),
    # overwrite the AI's guess with the real, computed one. When Angel One
    # isn't configured, mark every frame "unavailable" instead of silently
    # keeping the AI's made-up numbers.
    multi_tf = market_data.get("multi_timeframe", {})
    real_tf = {}
    label_map = {"5min": "5min", "15min": "15min", "1hr": "1hr"}
    for label in label_map:
        frame = multi_tf.get(label, {})
        real_tf[label] = frame.get("trend", "unavailable")
    result["timeframe_trend"] = real_tf
    # Single source of truth: Strategy and Analysis use the same final
    # machine action after scoring, lifecycle, confluence, regime and gates.
    if persisted_state_override is None:
        persisted_state = await load_signal_state(symbol.upper())
        dec = _overlay_persisted_lifecycle(dec, persisted_state)
    else:
        # Reuse lifecycle state already applied by the Strategy request.
        persisted_state = persisted_state_override
        dec = market_data.get("decision", dec)
    market_data["decision"] = dec

    # HISTORY_LIFECYCLE_PERSIST_FIX_20261002
    # The persistent lifecycle overlay above is authoritative.  Synchronize
    # the result object before final action/recommendation construction so
    # historical AnalysisResult rows cannot retain the pre-overlay,
    # process-local lifecycle fields.
    result["signal_lifecycle"] = dec.get("signal_lifecycle", "WAIT")
    result["signal_candidate"] = dec.get("signal_candidate", "NONE")
    result["signal_confirmations"] = dec.get("signal_confirmations", 0)
    result["signal_reversal_confirmations"] = dec.get("signal_reversal_confirmations", 0)
    result["signal_active_side"] = dec.get("signal_active_side", "NONE")
    result["hard_gated"] = dec.get("hard_gated", False)

    try:
        from app.api.routes.strategy import resolve_final_strategy_action
        final_action = await resolve_final_strategy_action(
            symbol.upper(), market_data, persisted_state
        )
    except Exception:
        final_action = {
            "best_strategy": "WAIT",
            "signal_action": "WAIT",
        }

    result["best_strategy"] = final_action["best_strategy"]
    result["signal_action"] = final_action["signal_action"]
    # Persist the complete candidate score board so Analysis history,
    # Accuracy, and downstream consumers see the same strategy source-of-truth
    # produced by resolve_final_strategy_action().
    result["candidates"] = dict(final_action.get("candidates") or {})
    # V2: expose the same confluence payload used by Strategy/Recommend
    # so Analysis and Dashboard share the same confluence source.
    result["confluence"] = final_action.get("confluence", {})
    # V3_GATE_HISTORY_PERSIST_20260923
    result["v3_gate"] = final_action.get("v3_gate", {})
    result["fresh_entry_allowed"] = (
        final_action.get("fresh_entry_allowed") is True
    )
    if isinstance(final_action.get("regime"), dict):
        result["market_regime"] = final_action["regime"]

    result["multi_timeframe"] = multi_tf

    # SESSION_STATE_API_EXPOSE_20260924
    # MarketAnalyzer already computes this observation/context layer.
    # Expose the exact same payload to Analysis without changing scoring,
    # lifecycle, entry gates, or strategy selection.
    result["session_state"] = market_data.get("session_state", {})

    # SESSION_CONTEXT_API_EXPOSE_STAGE2A_20260924
    # DecisionEngine adds this observation-only context. Expose it to the
    # Analysis API without modifying any score, gate, lifecycle, or strategy.
    result["session_context"] = dec.get(
        "session_context",
        market_data.get("session_context", {}),
    )

    # Tamil indicator explanations, scenarios+invalidation, signal-strength
    # framing, expiry/market-status — the fields the Analysis page needs to
    # actually build the layout the user asked for (see review doc §7, §8,
    # §17, §19, §20).
    result["tamil_indicators"]       = market_data.get("tamil_indicators", [])
    result["scenarios"]              = dec.get("scenarios", [])
    result["signal_strength"]        = dec.get("signal_strength", dec.get("confidence", 0))
    result["data_completeness_pct"]  = dec.get("data_completeness_pct", 100)
    result["volatility_regime"]      = dec.get("volatility_regime", "unknown")
    # Review #4: market-regime-adaptive weighting result — see decision_engine.py.
    result["market_regime"]          = dec.get("market_regime", "unknown")
    result["market_regime_confidence"] = dec.get("market_regime_confidence", "LOW")
    result["market_regime_reasons"]  = dec.get("market_regime_reasons", [])
    result["volatility_label"]       = dec.get("volatility_label", "")
    result["oi_change_tracked"]      = market_data.get("oi_change_tracked", {"available": False})
    result["support_resistance"]     = market_data.get("support_resistance", {})
    result["market_open"]            = market_data.get("market_open") is True
    result["expiry_risk"]            = market_data.get("expiry_risk", {})
    # Review #1: the actual option-premium tracking target for this signal
    # — see _pick_recommended_option docstring.
    result["recommended_option"] = _pick_recommended_option(
        market_data, dec, result.get("signal_action", "")
    )
    result = await _apply_entry_risk_authority(result, market_data)

    # Review #4/#45/#46:
    # "Signal Strength 85% ≠ 85% win probability."
    #
    # Calibration is meaningful only for an ACTIONABLE current signal.
    # NONE / WATCH / WAIT states deliberately receive no historical
    # calibration lookup because there is no active directional signal
    # for the user to evaluate.
    #
    # The historical calibration engine itself is also restricted to
    # independent actionable CONFIRMED/HOLD episodes.
    current_side = str(
        result.get("preferred_side") or dec.get("preferred_side") or "NONE"
    ).upper()
    current_lifecycle = str(
        result.get("signal_lifecycle") or dec.get("signal_lifecycle") or "WAIT"
    ).upper()
    current_active_side = str(
        result.get("signal_active_side") or dec.get("signal_active_side") or "NONE"
    ).upper()

    current_signal_actionable = (
        current_side in ("CALL", "PUT")
        and current_lifecycle.startswith(("CONFIRMED_", "HOLD_"))
        and current_active_side == current_side
        and not bool(dec.get("hard_gated", False))
    )

    if current_signal_actionable:
        try:
            from app.services.signal_accuracy import calibrate_confidence  # local import — avoid circular import at module load
            result["confidence_calibration"] = await calibrate_confidence(
                symbol,
                result.get("signal_strength", 0),
            )
        except Exception as _cal_err:
            logger.warning(
                f"Confidence calibration unavailable for {symbol}: {_cal_err}"
            )
            result["confidence_calibration"] = {
                "signal_strength": result.get("signal_strength", 0),
                "confidence_bucket": None,
                "historical_win_rate_pct": None,
                "sample_size": 0,
                "episodes": 0,
                "graded": 0,
                "insufficient_data": True,
                "calibration_basis": "ACTIONABLE_INDEPENDENT_EPISODES",
                "reason": "Historical calibration currently unavailable.",
                "disclaimer": (
                    "Signal Strength ஒரு win probability இல்லை — "
                    "historical calibration தற்போது கிடைக்கவில்லை."
                ),
            }
    else:
        result["confidence_calibration"] = {
            "signal_strength": result.get("signal_strength", 0),
            "confidence_bucket": None,
            "historical_win_rate_pct": None,
            "sample_size": 0,
            "episodes": 0,
            "graded": 0,
            "insufficient_data": True,
            "calibration_basis": "ACTIONABLE_INDEPENDENT_EPISODES",
            "current_signal_actionable": False,
            "reason": (
                "Calibration is shown only for an active CONFIRMED/HOLD "
                "CALL or PUT signal."
            ),
            "disclaimer": (
                "Signal Strength ஒரு win probability இல்லை — "
                "WATCH/WAIT/NONE நிலையில் actionable historical "
                "calibration காட்டப்படாது."
            ),
        }

    # Preserve the exact market snapshot timestamp inside the persisted
    # analysis result.  AnalysisResult.timestamp is DB write time and must
    # not be used as the market-snapshot identity.
    result["market_snapshot_timestamp"] = market_data.get("timestamp")
    result["symbol"] = symbol
    result.update(_metric_quality_fields(market_data))
    result["data_quality"] = {
        "futures_premium": market_data.get("futures_premium_status", "unavailable"),
        "global_market":   market_data.get("global_status", "unavailable"),
        "gift_nifty":      market_data.get("gift_status", "unavailable"),
        "fii":             market_data.get("fii_status", "unavailable"),
        "dii":             market_data.get("dii_status", "unavailable"),
        "technicals_source": market_data.get("technical_data_source", "unknown"),
    }
    # BUG FIX: dashboard's "Futures" metric card needs the actual number,
    # not just the live/unavailable status string above. market_analyzer
    # already computes this (futures_premium / futures_premium_pct) — it
    # just never made it into any route's response before now.
    result["futures_premium_value"]     = market_data.get("futures_premium", 0.0)
    result["futures_premium_pct_value"] = market_data.get("futures_premium_pct", 0.0)

    # Foreground HTTP analysis persists AI history through
    # save_analysis_result(); its 5-minute throttle prevents duplicate rows.
    # history_collector.py remains responsible for scheduled collection
    # outside market hours.
    return _finalize_ai_analysis_result(result)


def _finalize_ai_analysis_result(result: dict) -> dict:
    """Expose directional contract details only for a fresh authorized entry."""
    lifecycle = str(
        result.get("signal_lifecycle", "WAIT") or "WAIT"
    ).upper()
    action = str(
        result.get("signal_action", result.get("action", "WAIT")) or "WAIT"
    ).strip().upper()

    gate = result.get("entry_gate") or result.get("v3_gate") or {}
    if not isinstance(gate, dict):
        gate = {}

    regime = result.get("market_regime")
    regime_blocked = bool(result.get("market_regime_no_trade")) or bool(
        isinstance(regime, dict)
        and (
            regime.get("hard_no_trade", False)
            or regime.get("no_trade", False)
        )
    )

    from app.services.entry_risk_authority import is_entry_risk_approved

    authority = result.get("entry_risk_authority")
    risk_authorized = (
        action in ("BUY CE", "BUY PE")
        and result.get("fresh_entry_allowed") is True
        and is_entry_risk_approved(authority)
        and authority.get("action") == action
    )

    fresh_entry_allowed = (
        risk_authorized
        and lifecycle in ("CONFIRMED_CALL", "CONFIRMED_PUT")
        and gate.get("allowed") is True
        and gate.get("fresh_entry_allowed") is True
        and not bool(result.get("hard_gated", False))
        and not regime_blocked
    )
    result["fresh_entry_allowed"] = bool(fresh_entry_allowed)

    # Preserve only the explicitly supported neutral Iron Condor context.
    # It is not a directional CALL/PUT entry recommendation.
    detail = result.get("strategy_detail")
    detail_name = ""
    if isinstance(detail, dict):
        detail_name = str(
            detail.get("name") or detail.get("strategy") or ""
        ).strip().casefold()

    regime_name = ""
    if isinstance(regime, dict):
        regime_name = str(
            regime.get("regime") or regime.get("name") or ""
        ).strip().upper()
    elif regime is not None:
        regime_name = str(regime).strip().upper()

    preserve_neutral_combo = (
        action == "WAIT"
        and (
            lifecycle == "WAIT"
            or lifecycle.startswith("WATCH_")
        )
        and detail_name == "iron condor"
        and regime_name in ("RANGE", "LOW_VOLATILITY")
        and not regime_blocked
        and not bool(result.get("hard_gated", False))
    )

    if not fresh_entry_allowed:
        result["recommended_option"] = None
        result["recommended_strike"] = "NONE"
        result["price_levels"] = None
        if not preserve_neutral_combo:
            result["strategy_detail"] = None

    return result


@router.get("/ai/{symbol}", response_model=AIAnalysisRouteResponse)
async def ai_analysis(
    symbol: str,
    expiry: str = None,
    cache_bust: str = "",
    analyzer: MarketAnalyzer = Depends(get_analyzer),
    ai: AIEngine = Depends(get_ai_engine),
):
    """`expiry`: optional, e.g. "28-Aug-2025" (one of option_chain.all_expiries).
    Omit for the nearest expiry (previous/default behaviour)."""
    result = await build_ai_analysis(
        symbol,
        analyzer,
        ai,
        expiry=expiry,
        cache_bust=cache_bust,
    )

    # build_ai_analysis() already loaded the authoritative lifecycle snapshot,
    # applied the read-only overlay, and computed best_strategy/signal_action
    # from that same snapshot. Re-reading persistent state here could race with
    # a concurrent lifecycle update and produce mixed fields such as
    # BUY CE + WAIT. Keep this HTTP response on the single snapshot used to
    # construct the result.
    
    # Do not expose a raw-direction option recommendation while the
    # persistent lifecycle is only WATCH/WAIT.
    lifecycle = str(result.get("signal_lifecycle", "WAIT") or "WAIT").upper()
    if (
        lifecycle == "WAIT"
        or lifecycle.startswith("WATCH_")
        or "_REVERSAL_WATCH_" in lifecycle
    ):
        result["recommended_option"] = None

    # HISTORY_LIFECYCLE_PERSIST_FIX_20261002
    # Persist only after the final read-only lifecycle/recommendation overlay,
    # so Analysis history and the API response share the same decision object.
    await save_analysis_result(symbol.upper(), "ai", result)

    return result


@router.get("/decision/{symbol}", response_model=AnalysisDecisionResponse)
async def rule_decision(symbol: str, analyzer: MarketAnalyzer = Depends(get_analyzer)):
    """Rule engine analysis மட்டும் — AI இல்லாமல் fast. Bias/probability/risk
    காட்டும், buy/sell instruction எதுவும் தராது."""
    try:
        data = await analyzer.get_full_market_overview(symbol)
        dec  = data.get("decision", {})
        return {
            "symbol":               symbol,
            "market_bias":          dec.get("market_bias", "Sideways"),
            "bullish_probability":  dec.get("bullish_probability", 50),
            "bearish_probability":  dec.get("bearish_probability", 50),
            "preferred_side":       dec.get("preferred_side", "NONE"),
            "signal_lifecycle":    dec.get("signal_lifecycle", "WAIT"),
            "signal_candidate":    dec.get("signal_candidate", "NONE"),
            "signal_confirmations": dec.get("signal_confirmations", 0),
            "signal_reversal_confirmations": dec.get("signal_reversal_confirmations", 0),
            "signal_active_side":  dec.get("signal_active_side", "NONE"),
            "recommended_strike":   dec.get("recommended_strike", "NONE"),
            "bull_score":           dec.get("bull_score", 0),
            "bear_score":           dec.get("bear_score", 0),
            "confidence":           dec.get("confidence", 0),
            "signal_strength":      dec.get("signal_strength", dec.get("confidence", 0)),
            "forecast":             dec.get("forecast", "Neutral"),
            "volatility_regime":    dec.get("volatility_regime", "unknown"),
            "volatility_label":     dec.get("volatility_label", ""),
            "risk":                 dec.get("risk", "Medium"),
            "reasons":              dec.get("reasons", []),
            "strategy":             dec.get("strategy", ""),
            "strategy_reason":      dec.get("strategy_reason", ""),
            "strategy_detail":      dec.get("strategy_detail"),
            "price_levels":         dec.get("price_levels"),
            "scenarios":            dec.get("scenarios", []),
            "pcr":                  data.get("pcr", 0),
            "max_pain":             data.get("max_pain", 0),
            "vix":                  data.get("vix", 0),
            "pcr_quality":          data.get("pcr_quality", {}),
            "vix_quality":          data.get("vix_quality", {}),
            "option_chain_valid":   data.get("option_chain_valid", False),
            "rsi":                  data.get("rsi", 50),
            "macd":                 data.get("macd", {}),
            "technicals":           data.get("technicals", {}),
            "multi_timeframe":      data.get("multi_timeframe", {}),
            "session_state":        data.get("session_state", {}),
            "oi_change_tracked":    data.get("oi_change_tracked", {"available": False}),
            "support_resistance":   data.get("support_resistance", {}),
            "tamil_indicators":     data.get("tamil_indicators", []),
            "option_volume":        data.get("option_volume", {}),
            "market_open":          data.get("market_open") is True,
            "expiry_risk":          data.get("expiry_risk", {}),
            "data_quality": {
                "futures_premium": data.get("futures_premium_status", "unavailable"),
                "global_market":   data.get("global_status", "unavailable"),
                "gift_nifty":      data.get("gift_status", "unavailable"),
                "fii":             data.get("fii_status", "unavailable"),
                "dii":             data.get("dii_status", "unavailable"),
                "technicals_source": data.get("technical_data_source", "unknown"),
            },
            "disclaimer":           "Informational analysis only — not investment advice.",
        }
    except MarketDataError as e:
        raise HTTPException(502, detail=str(e))
