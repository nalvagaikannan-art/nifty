from fastapi import APIRouter, Depends, HTTPException
from app.services.market_analyzer import MarketAnalyzer
from app.services.ai_engine import AIEngine
from app.services.history_service import save_analysis_result
from app.exceptions import AIProviderError, MarketDataError
from app.api.deps import get_analyzer, get_ai_engine
from app.utils.helpers import safe_float
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


def _pick_recommended_option(market_data: dict, dec: dict) -> dict:
    """Picks ONE concrete strike (contract) matching the rule engine's
    preferred_side + recommended_strike label (ATM / ATM+1), with a
    mid-price entry estimate. Review #1: this is what lets the accuracy
    engine grade the SIGNAL AN OPTION BUYER WOULD ACTUALLY TAKE — a specific
    contract's premium — instead of only grading spot direction, which the
    review correctly points out can be "right" while the option itself
    loses money to theta/IV crush. Reuses strategy.py's own strike-picking
    (`_pick_strikes` — liquidity filter, mid-price, Greeks) as the single
    source of truth instead of a second, drifting implementation."""
    side = (dec.get("preferred_side") or "NONE").upper()
    if side not in ("CALL", "PUT"):
        return {"available": False, "reason": "No directional signal (NONE) — nothing to track premium for"}

    chain = market_data.get("option_chain") or {}
    spot = (market_data.get("spot") or {}).get("price", 0)
    if not chain.get("data") or spot <= 0:
        return {"available": False, "reason": "Option chain unavailable"}

    from app.api.routes.strategy import _pick_strikes  # local import — see module docstring on _pick_recommended_option
    atr = safe_float((market_data.get("technicals") or {}).get("atr", 0))
    picks = _pick_strikes(chain, is_call=(side == "CALL"), spot=spot, atr=atr)
    if not picks:
        return {"available": False, "reason": "No liquid strike found near ATM"}

    # recommended_strike label from decision_engine is "ATM" or "ATM+1" —
    # _pick_strikes' first ("Best Strike") pick IS the ATM+1/ATM-1 liquidity
    # pick already; fall back to it either way since it's always liquid.
    chosen = picks[0]
    return {
        "available":    True,
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
        "liquidity_note": chosen.get("note"),
    }


async def build_ai_analysis(symbol: str, analyzer: MarketAnalyzer, ai: AIEngine, expiry: str = None) -> dict:
    """Builds the exact same result shape /api/analysis/ai/{symbol} returns —
    factored out so both the HTTP route AND the background history collector
    (app/services/history_collector.py) save identical, compatible rows.
    Previously this logic lived only inside the route handler, which meant
    AnalysisResult rows (needed by the Accuracy page / signal_accuracy.py)
    only got written when a human happened to have the Analysis page open —
    the background collector calls this function on its own schedule so
    history keeps accumulating even with zero browser tabs open."""
    try:
        market_data = await analyzer.get_full_market_overview(symbol, expiry=expiry)
    except MarketDataError as e:
        raise HTTPException(502, detail=f"Market data unavailable: {e}")

    try:
        result = await ai.analyze_market(market_data)
    except AIProviderError as e:
        # AI fail ஆனாலும் rule-engine result return செய் — analysis only,
        # no buy/sell instruction anywhere in this fallback either.
        dec = market_data.get("decision", {})
        result = {
            "market_bias":         dec.get("market_bias", "Sideways"),
            "bullish_probability": dec.get("bullish_probability", 50),
            "bearish_probability": dec.get("bearish_probability", 50),
            "preferred_side":      dec.get("preferred_side", "NONE"),
            "market_trend":        market_data.get("trend", "sideways"),
            "reason":              "AI unavailable. Rule-engine analysis shown.",
            "key_factors":         [r for r in dec.get("reasons", [])[:5]],
            "timeframe_trend":     {"5min": "N/A", "15min": "N/A", "1hr": "N/A"},
            "support":             market_data.get("support_resistance", {}).get("support", []),
            "resistance":          market_data.get("support_resistance", {}).get("resistance", []),
            "risk":                dec.get("risk", "Medium"),
            "vix":                 market_data.get("vix", 0),
            "pcr":                 market_data.get("pcr", 0),
            "bull_score":          dec.get("bull_score", 0),
            "bear_score":          dec.get("bear_score", 0),
            "confidence":          dec.get("confidence", 0),
            "forecast":            dec.get("forecast", "Neutral"),
            "ai_agrees":           True,
            "_provider":           "rule_engine_only",
            "disclaimer":          "Informational analysis only — not investment advice.",
        }

    # Full reasons list + strategy/price-level detail சேர்க்க
    dec = market_data.get("decision", {})
    result["all_reasons"]        = dec.get("reasons", [])
    result["recommended_strike"] = dec.get("recommended_strike", "NONE")
    result["signal_lifecycle"] = dec.get("signal_lifecycle", "WAIT")
    result["signal_candidate"] = dec.get("signal_candidate", "NONE")
    result["signal_confirmations"] = dec.get("signal_confirmations", 0)
    result["signal_reversal_confirmations"] = dec.get("signal_reversal_confirmations", 0)
    result["signal_active_side"] = dec.get("signal_active_side", "NONE")
    result["strategy"]           = dec.get("strategy", "")
    result["strategy_reason"]    = dec.get("strategy_reason", "")
    result["strategy_detail"]    = dec.get("strategy_detail")
    result["price_levels"]       = dec.get("price_levels")
    result["max_pain"]           = market_data.get("max_pain", 0)
    result["technicals"]         = market_data.get("technicals", {})
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
    result["multi_timeframe"] = multi_tf

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
    result["market_open"]            = market_data.get("market_open", True)
    result["expiry_risk"]            = market_data.get("expiry_risk", {})
    # Review #1: the actual option-premium tracking target for this signal
    # — see _pick_recommended_option docstring.
    result["recommended_option"] = _pick_recommended_option(market_data, dec)

    # Review #4/#45/#46: "Signal Strength 85% ≠ 85% win probability." Attach
    # the historical win-rate for THIS confidence's bucket (computed from
    # past graded signals) right alongside the live signal, so the UI can
    # show the calibration gap instead of implying the raw score is itself
    # a probability. Best-effort: a fresh DB / no history yet must not break
    # the analysis response, so this is fully guarded.
    try:
        from app.services.signal_accuracy import calibrate_confidence  # local import — avoid circular import at module load
        result["confidence_calibration"] = await calibrate_confidence(
            symbol, result.get("signal_strength", 0)
        )
    except Exception as _cal_err:
        logger.warning(f"Confidence calibration unavailable for {symbol}: {_cal_err}")
        result["confidence_calibration"] = {
            "signal_strength": result.get("signal_strength", 0),
            "historical_win_rate_pct": None,
            "sample_size": 0,
            "insufficient_data": True,
            "disclaimer": "Signal Strength ஒரு win probability இல்லை — historical calibration தற்போது கிடைக்கவில்லை.",
        }

    result["symbol"] = symbol
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

    await save_analysis_result(symbol, "ai", result)
    return result


@router.get("/ai/{symbol}")
async def ai_analysis(
    symbol: str,
    expiry: str = None,
    analyzer: MarketAnalyzer = Depends(get_analyzer),
    ai: AIEngine = Depends(get_ai_engine),
):
    """`expiry`: optional, e.g. "28-Aug-2025" (one of option_chain.all_expiries).
    Omit for the nearest expiry (previous/default behaviour)."""
    return await build_ai_analysis(symbol, analyzer, ai, expiry=expiry)


@router.get("/decision/{symbol}")
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
            "rsi":                  data.get("rsi", 50),
            "macd":                 data.get("macd", {}),
            "technicals":           data.get("technicals", {}),
            "multi_timeframe":      data.get("multi_timeframe", {}),
            "oi_change_tracked":    data.get("oi_change_tracked", {"available": False}),
            "support_resistance":   data.get("support_resistance", {}),
            "tamil_indicators":     data.get("tamil_indicators", []),
            "option_volume":        data.get("option_volume", {}),
            "market_open":          data.get("market_open", True),
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
