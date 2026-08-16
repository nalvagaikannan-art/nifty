"""
Rolling health metrics — NSE block-rate tracking (CODE_REVIEW.md #19/#20)
===========================================================================
A single process-lifetime, in-memory rolling window per named source (e.g.
"nse", "angel_one", "ai_gemini") that counts outcomes (ok / blocked /
transient_error / other_error) over the last N calls and the last T seconds.

This is intentionally simple — no external metrics service required. It
answers the two questions CODE_REVIEW.md's checklist asked for:
  1. "What fraction of recent NSE calls are getting blocked (401/403)?" —
     an early signal the server's IP/session is getting rate-limited, well
     before every single request starts failing.
  2. "Should something be alerted?" — optional: if ALERT_WEBHOOK_URL is
     configured, a simple JSON POST fires when a source's block-rate crosses
     the configured threshold (default 30% of the last 20 calls). Webhook
     failures are logged, never raised — alerting must never break the
     request that triggered it.

Multi-process deployments (see REDIS_URL) each track their own metrics here
— this is a per-process signal, not a fleet-wide one. Good enough for a
single server; a fleet would want these counters exported to Prometheus/
whatever instead, which is out of scope for this lightweight tracker.
"""
import time
import asyncio
import logging
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

_WINDOW_CALLS = 50          # how many recent calls each source remembers
_ALERT_WINDOW_CALLS = 20    # how many of the most recent calls the alert check looks at
_ALERT_THRESHOLD = 0.30     # block-rate fraction that triggers an alert
_ALERT_COOLDOWN_SECONDS = 900  # don't re-alert for the same source more than every 15 min

# source -> deque[(timestamp, outcome)]
_history: Dict[str, Deque[Tuple[float, str]]] = {}
_last_alert_at: Dict[str, float] = {}


def record(source: str, outcome: str) -> None:
    """outcome: 'ok' | 'blocked' | 'transient_error' | 'other_error'"""
    hist = _history.setdefault(source, deque(maxlen=_WINDOW_CALLS))
    hist.append((time.time(), outcome))
    if outcome == "blocked":
        _maybe_alert(source, hist)


def snapshot(source: Optional[str] = None) -> Dict:
    """Returns per-source stats. If `source` given, just that one."""
    sources = [source] if source else list(_history.keys())
    out = {}
    for s in sources:
        hist = _history.get(s)
        if not hist:
            out[s] = {"calls": 0, "block_rate": None, "last_outcome": None}
            continue
        total = len(hist)
        blocked = sum(1 for _, o in hist if o == "blocked")
        transient = sum(1 for _, o in hist if o == "transient_error")
        ok = sum(1 for _, o in hist if o == "ok")
        out[s] = {
            "calls_tracked": total,
            "ok": ok,
            "blocked": blocked,
            "transient_error": transient,
            "other_error": total - ok - blocked - transient,
            "block_rate": round(blocked / total, 3) if total else None,
            "last_outcome": hist[-1][1],
            "last_outcome_age_seconds": round(time.time() - hist[-1][0], 1),
        }
    return out


def _maybe_alert(source: str, hist: Deque[Tuple[float, str]]) -> None:
    recent = list(hist)[-_ALERT_WINDOW_CALLS:]
    if len(recent) < _ALERT_WINDOW_CALLS:
        return  # not enough samples yet to judge a rate
    block_rate = sum(1 for _, o in recent if o == "blocked") / len(recent)
    if block_rate < _ALERT_THRESHOLD:
        return

    now = time.time()
    if now - _last_alert_at.get(source, 0) < _ALERT_COOLDOWN_SECONDS:
        return  # cooldown — don't spam
    _last_alert_at[source] = now

    msg = (f"⚠️ {source}: block rate {block_rate:.0%} over last {len(recent)} calls "
           f"— possible IP/session block. Check DATA UNAVAILABLE frequency.")
    logger.warning(msg)

    if settings.alert_webhook_url:
        try:
            asyncio.get_event_loop().create_task(_send_webhook(source, block_rate, msg))
        except RuntimeError:
            pass  # no running loop (e.g. called from sync/test context) — skip webhook


async def _send_webhook(source: str, block_rate: float, message: str) -> None:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(settings.alert_webhook_url, json={
                "source": source,
                "block_rate": block_rate,
                "message": message,
                "app": "nifty-ai-analyzer",
            })
    except Exception as e:
        logger.warning(f"Alert webhook delivery failed (metrics still tracked locally): {e}")
