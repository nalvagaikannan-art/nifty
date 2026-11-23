"""Low-impact decision diagnostics for explaining delayed/blocked signals.

This module is deliberately observational: it never feeds values back into
scoring, gating, lifecycle, or API responses. One JSONL event per symbol per
60 seconds is written beside the configured SQLite database.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRACE_INTERVAL_SECONDS = 60.0
TRACE_MAX_BYTES = 8 * 1024 * 1024
_TRACE_LOCK = threading.Lock()
_LAST_WRITE: dict[str, float] = {}
_LAST_ERROR_LOG: dict[str, float] = {}


def _safe_text(value: Any, limit: int = 400) -> str | None:
    if value is None:
        return None
    try:
        result = str(value)
    except Exception:
        return None
    return result[:limit]


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            scalar = item()
        except Exception:
            return None
        return scalar if isinstance(scalar, bool) else None
    return None


def _configured_trace_path() -> Path:
    override = os.environ.get("DECISION_TRACE_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    # Use the same data directory as the configured SQLite database. In the
    # production layout this resolves to <repo>/data/decision_trace.jsonl.
    from app.config import settings

    db_path = Path(settings.sqlite_db_path).expanduser()
    if str(db_path) == ":memory:":
        db_path = Path.cwd() / "data" / "analyzer.db"
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    return db_path.resolve().parent / "decision_trace.jsonl"


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < TRACE_MAX_BYTES:
            return
        backup = path.with_name(path.name + ".1")
        backup.unlink(missing_ok=True)
        os.replace(path, backup)
    except OSError:
        # Rotation failure must not interrupt the analysis path. The append
        # below may still succeed; otherwise the outer fail-open handler logs.
        logger.warning("Decision trace rotation failed for %s", path, exc_info=True)


def _mtf_snapshot(multi_tf: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(multi_tf, dict):
        multi_tf = {}
    result: dict[str, dict[str, Any]] = {}
    for timeframe in ("5min", "15min", "1hr"):
        frame = multi_tf.get(timeframe)
        if not isinstance(frame, dict):
            frame = {}
        result[timeframe] = {
            "source": _safe_text(frame.get("data_source"), 80),
            "fresh": _safe_bool(frame.get("fresh")),
            "age_minutes": _safe_number(frame.get("freshness_minutes")),
            "timestamp": _safe_text(frame.get("last_timestamp"), 80),
            "freshness_reason": _safe_text(frame.get("freshness_reason"), 180),
            "trend": _safe_text(frame.get("trend"), 40),
        }
    return result


def _indicator_snapshot(indicators: Any) -> list[dict[str, Any]]:
    if not isinstance(indicators, (list, tuple)):
        return []
    result = []
    for item in indicators:
        if not isinstance(item, dict):
            continue
        result.append({
            "name": _safe_text(item.get("name"), 60),
            "direction": _safe_text(item.get("direction"), 20),
            "points": _safe_number(item.get("points")) or 0,
            "reason": _safe_text(item.get("reason"), 300),
        })
    return result


def _reason_codes(
    *,
    hard_gated: bool,
    market_open: bool,
    regime: str,
    spot_valid: bool,
    chain_available: bool,
    technical_source_valid: bool,
    mtf_live_valid: bool,
    mtf: dict[str, dict[str, Any]],
    margin: float,
    raw_side: str,
    lifecycle_state: str,
    confirmations: int,
    required_confirmations: int,
    gate_reasons: list[str],
    option_chain_quality: dict[str, Any],
) -> list[str]:
    codes: list[str] = []
    if not market_open:
        codes.append("MARKET_CLOSED_OR_MARKET_FLAG_MISSING")
    if regime == "NO_TRADE":
        codes.append("MARKET_REGIME_NO_TRADE")
    if not spot_valid:
        codes.append("INVALID_SPOT")
    if not chain_available:
        codes.append("OPTION_CHAIN_UNAVAILABLE")
    if option_chain_quality and (
        option_chain_quality.get("available") is False
        or option_chain_quality.get("oi_usable") is False
        or option_chain_quality.get("volume_usable") is False
    ):
        codes.append("OPTION_CHAIN_QUALITY_LIMITED")
    if not technical_source_valid:
        codes.append("TECHNICAL_SOURCE_NOT_INTRADAY_MTF")
    if not mtf_live_valid:
        codes.append("MTF_MISSING_OR_STALE")
        for tf, frame in mtf.items():
            if frame.get("source") not in ("angel_one_intraday", "zerodha_intraday"):
                codes.append(f"{tf.upper()}_SOURCE_UNAVAILABLE")
            elif frame.get("fresh") is not True:
                codes.append(f"{tf.upper()}_STALE")
    if any("MTF conflict" in reason for reason in gate_reasons):
        codes.append("MTF_DIRECTION_CONFLICT")
    if any("RANGE CALL blocked" in reason for reason in gate_reasons):
        codes.append("RANGE_CALL_STRUCTURE_OR_MARGIN_GUARD")
    if abs(margin) < 10:
        codes.append("DIRECTIONAL_MARGIN_BELOW_10")
    elif raw_side in ("CALL", "PUT") and abs(margin) < 12:
        codes.append("CONFIRMATION_MARGIN_BELOW_12")
    if lifecycle_state.startswith("WATCH_") and confirmations < required_confirmations:
        codes.append("LIFECYCLE_CONFIRMATIONS_PENDING")
    if hard_gated and not gate_reasons:
        codes.append("HARD_GATE_ACTIVE_REASON_NOT_CLASSIFIED")
    if not codes:
        codes.append("NO_BLOCKING_GATE_DETECTED")
    return list(dict.fromkeys(codes))


def record_decision_trace(
    *,
    market_data: dict[str, Any],
    indicators: list[dict[str, Any]],
    raw_scores: dict[str, Any],
    bull_score: Any,
    bear_score: Any,
    margin: Any,
    data_completeness_pct: Any,
    data_availability: dict[str, Any],
    regime: str,
    regime_confidence: str,
    regime_reasons: list[str],
    regime_no_trade_reason: str,
    session_context: dict[str, Any],
    raw_preferred_side: str,
    preferred_side: str,
    lifecycle: dict[str, Any],
    hard_gated: bool,
    gate_reasons: list[str],
    thresholds: dict[str, Any],
    gate_state: dict[str, Any],
    trace_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Append one allowlisted diagnostic event; return False when throttled.

    All I/O errors are swallowed intentionally so diagnostics can never take
    down or delay the market decision path. The function never mutates inputs.
    """
    symbol = _safe_text(market_data.get("symbol") or "UNKNOWN", 24).upper()
    path = Path(trace_path).expanduser().resolve() if trace_path else _configured_trace_path()
    key = f"{path}:{symbol}"
    now_mono = time.monotonic()

    with _TRACE_LOCK:
        previous = _LAST_WRITE.get(key)
        if previous is not None and now_mono - previous < TRACE_INTERVAL_SECONDS:
            return False

        spot = market_data.get("spot")
        spot = spot if isinstance(spot, dict) else {}
        technicals = market_data.get("technicals")
        technicals = technicals if isinstance(technicals, dict) else {}
        multi_tf = _mtf_snapshot(market_data.get("multi_timeframe"))
        pcr_quality = market_data.get("pcr_quality")
        if not isinstance(pcr_quality, dict):
            pcr_quality = {}
        option_quality = market_data.get("option_chain_quality")
        if not isinstance(option_quality, dict):
            option_quality = pcr_quality.get("chain_quality")
        if not isinstance(option_quality, dict):
            option_quality = {}

        now_dt = datetime.now(timezone.utc)
        now_utc = now_dt.isoformat()
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        gate_reasons = [_safe_text(reason, 400) for reason in (gate_reasons or [])]
        gate_reasons = [reason for reason in gate_reasons if reason]
        margin_value = _safe_number(margin) or 0
        raw_side = _safe_text(raw_preferred_side, 20) or "NONE"
        lifecycle_state = _safe_text(lifecycle.get("state"), 40) or "WAIT"
        confirmation_count = int(_safe_number(lifecycle.get("confirmations")) or 0)
        required_confirmations = int(_safe_number(thresholds.get("required_confirmations")) or 3)
        confirmation_interval = _safe_number(
            thresholds.get("confirmation_min_interval_seconds")
        ) or 0
        last_confirmation_ts = _safe_number(lifecycle.get("last_confirmation_ts"))
        confirmation_wait_remaining = (
            max(0.0, last_confirmation_ts + confirmation_interval - now_dt.timestamp())
            if last_confirmation_ts and confirmation_interval > 0 else 0.0
        )
        market_open = spot.get("market_open") is True
        spot_valid = (_safe_number(spot.get("price")) or 0) > 0
        chain_available = (_safe_number(market_data.get("pcr")) or 0) > 0
        technical_source_valid = (
            market_data.get("technical_data_source") == "intraday_5min_ohlc"
            and all(
                frame.get("source") in ("angel_one_intraday", "zerodha_intraday")
                for frame in multi_tf.values()
            )
        )
        mtf_live_valid = all(
            frame.get("source") in ("angel_one_intraday", "zerodha_intraday")
            and frame.get("fresh") is True
            for frame in multi_tf.values()
        )

        event = {
            "schema_version": 1,
            "timestamp_utc": now_utc,
            "event_timestamp": _safe_text(
                market_data.get("market_snapshot_timestamp")
                or market_data.get("timestamp"), 80
            ),
            "symbol": symbol,
            "market_data": {
                "spot": _safe_number(spot.get("price")),
                "spot_source": _safe_text(
                    spot.get("data_source") or spot.get("source"), 80
                ),
                "spot_timestamp": _safe_text(
                    spot.get("exchange_timestamp") or spot.get("timestamp"), 80
                ),
                "spot_age_seconds": _safe_number(
                    spot.get("age_seconds") or spot.get("freshness_seconds")
                ),
                "technical_data_source": _safe_text(
                    market_data.get("technical_data_source"), 80
                ),
                "technical_timestamp": _safe_text(
                    market_data.get("technical_timestamp")
                    or market_data.get("market_snapshot_timestamp"), 80
                ),
                "pcr": _safe_number(market_data.get("pcr")),
                "option_chain_source": _safe_text(pcr_quality.get("source"), 80),
                "option_chain_fetched_at": _safe_text(pcr_quality.get("fetched_at"), 80),
                "option_chain_observed_at": _safe_text(pcr_quality.get("observed_at"), 80),
                "option_chain_unavailable_reason": _safe_text(pcr_quality.get("reason"), 180),
                "vix": _safe_number(market_data.get("vix")),
                "source_status": {
                    "futures_premium": _safe_text(market_data.get("futures_premium_status"), 40),
                    "global_market": _safe_text(market_data.get("global_status"), 40),
                    "gift_nifty": _safe_text(market_data.get("gift_status"), 40),
                    "fii": _safe_text(market_data.get("fii_status"), 40),
                    "dii": _safe_text(market_data.get("dii_status"), 40),
                    "vix": {
                        "source": _safe_text((market_data.get("vix_quality") or {}).get("source"), 80),
                        "available": _safe_bool((market_data.get("vix_quality") or {}).get("available")),
                        "fetched_at": _safe_text((market_data.get("vix_quality") or {}).get("fetched_at"), 80),
                        "reason": _safe_text((market_data.get("vix_quality") or {}).get("reason"), 160),
                    } if isinstance(market_data.get("vix_quality"), dict) else {},
                },
                "option_chain_quality": {
                    "reported": bool(option_quality),
                    "available": _safe_bool(option_quality.get("available")),
                    "oi_usable": _safe_bool(option_quality.get("oi_usable")),
                    "volume_usable": _safe_bool(option_quality.get("volume_usable")),
                    "oi_coverage_pct": _safe_number(option_quality.get("oi_coverage_pct")),
                    "volume_coverage_pct": _safe_number(option_quality.get("volume_coverage_pct")),
                    "reason": _safe_text(option_quality.get("reason"), 160),
                    "volume_reason": _safe_text(option_quality.get("volume_reason"), 160),
                },
                "multi_timeframe": multi_tf,
                "technicals": {
                    key: _safe_number(technicals.get(key))
                    for key in (
                        "vwap", "ema20", "ema50", "adx",
                        "di_plus", "di_minus", "atr", "volume_ratio",
                    )
                } | {
                    "supertrend": _safe_text(technicals.get("supertrend"), 40),
                    "volume_spike": _safe_bool(technicals.get("volume_spike")),
                },
                "rsi": _safe_number(market_data.get("rsi")),
                "macd": {
                    key: _safe_number((market_data.get("macd") or {}).get(key))
                    for key in ("macd", "signal", "histogram")
                } if isinstance(market_data.get("macd"), dict) else {},
            },
            "indicators": _indicator_snapshot(indicators),
            "scoring": {
                "bull_score": _safe_number(bull_score),
                "bear_score": _safe_number(bear_score),
                "margin": _safe_number(margin),
                "raw_bull_score": _safe_number(raw_scores.get("bull")),
                "raw_bear_score": _safe_number(raw_scores.get("bear")),
                "raw_neutral_score": _safe_number(raw_scores.get("neutral")),
                "data_completeness_pct": _safe_number(data_completeness_pct),
                "data_availability": {
                    str(k): bool(v) for k, v in (data_availability or {}).items()
                },
                "thresholds": {
                    str(k): _safe_number(v) for k, v in (thresholds or {}).items()
                },
            },
            "market_regime": {
                "regime": _safe_text(regime, 40),
                "confidence": _safe_text(regime_confidence, 24),
                "no_trade": regime == "NO_TRADE",
                "reasons": [
                    _safe_text(reason, 240) for reason in (regime_reasons or [])
                    if _safe_text(reason, 240)
                ],
                "no_trade_reason": _safe_text(regime_no_trade_reason, 240),
            },
            "session_context": {
                str(key): _safe_text(value, 80)
                for key, value in (session_context or {}).items()
                if key in {
                    "phase", "day_type", "environment", "volatility_state",
                    "transition_state", "vwap_relation", "trend_5m",
                    "trend_15m", "alignment",
                }
            } | {
                "state_confidence": _safe_number(
                    (session_context or {}).get("state_confidence")
                ),
            },
            "gates": {
                "hard_gated": bool(hard_gated),
                "state": {
                    str(k): _safe_bool(v) for k, v in (gate_state or {}).items()
                },
                "reason_codes": _reason_codes(
                    hard_gated=bool(hard_gated),
                    market_open=market_open,
                    regime=str(regime or ""),
                    spot_valid=spot_valid,
                    chain_available=chain_available,
                    technical_source_valid=technical_source_valid,
                    mtf_live_valid=mtf_live_valid,
                    mtf=multi_tf,
                    margin=margin_value,
                    raw_side=raw_side,
                    lifecycle_state=lifecycle_state,
                    confirmations=confirmation_count,
                    required_confirmations=required_confirmations,
                    gate_reasons=gate_reasons,
                    option_chain_quality=option_quality,
                ),
                "reasons": gate_reasons,
            },
            "lifecycle": {
                "raw_preferred_side": raw_side,
                "preferred_side": _safe_text(preferred_side, 20),
                "state": lifecycle_state,
                "candidate_side": _safe_text(lifecycle.get("candidate_side"), 20),
                "active_side": _safe_text(lifecycle.get("active_side"), 20),
                "confirmations": confirmation_count,
                "reversal_confirmations": int(
                    _safe_number(lifecycle.get("reversal_confirmations")) or 0
                ),
                "reason": _safe_text(lifecycle.get("reason"), 400),
                "last_confirmation_ts": last_confirmation_ts,
                "required_confirmations": required_confirmations,
                "confirmation_interval_seconds": confirmation_interval,
                "confirmation_wait_remaining_seconds": round(
                    confirmation_wait_remaining, 2
                ),
            },
        }

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path)
            serialized = json.dumps(
                event, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(serialized + "\n")
                    handle.flush()
            except BaseException:
                # fdopen owns the descriptor after successful construction.
                raise
            _LAST_WRITE[key] = now_mono
            return True
        except Exception:
            last_error = _LAST_ERROR_LOG.get(key, 0.0)
            if now_mono - last_error >= TRACE_INTERVAL_SECONDS:
                logger.warning(
                    "Decision trace write failed for symbol=%s; decision flow continues",
                    symbol,
                    exc_info=True,
                )
                _LAST_ERROR_LOG[key] = now_mono
            _LAST_WRITE[key] = now_mono
            return False
