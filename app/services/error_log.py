"""
Recent-errors ring buffer for the Accuracy page.

Why this exists: the Accuracy page's four API calls (status/signals/
premium/indicators) were firing in parallel and, on Render's free tier,
could crash the whole process under load — which meant the browser saw a
broken/empty HTTP response instead of a clean JSON error, and just showed
a generic "server error" / "Data unavailable" fallback with zero
diagnostic value.

This module gives every accuracy route a single call — `record_error()` —
to log a failure into a small in-memory ring buffer (last 20, per
process). `/api/accuracy/status/{symbol}` then returns this list under
`recent_errors`, so accuracy.html can render an actual warning ("Signal
accuracy failed 3 times in the last hour: <reason>") instead of a dead
end. Deliberately in-memory (no DB write) — this is operational
diagnostics, not data worth persisting across restarts, and adding a DB
write on the error path of a route that's already failing risks making
things worse under load.
"""
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List

_MAX_ERRORS = 20
_recent_errors: Deque[Dict] = deque(maxlen=_MAX_ERRORS)


def record_error(endpoint: str, symbol: str, detail: str) -> None:
    _recent_errors.append({
        "endpoint": endpoint,
        "symbol": symbol,
        "detail": detail,
        "at_utc": datetime.now(timezone.utc).isoformat(),
    })


def recent_errors(symbol: str = None, limit: int = 5) -> List[Dict]:
    items = list(_recent_errors)
    if symbol:
        items = [e for e in items if e["symbol"] == symbol]
    return items[-limit:][::-1]  # most recent first
