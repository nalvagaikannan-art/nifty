"""
Angel One SmartAPI Integration
================================
Angel One SmartAPI மூலம் live market data, option chain, login/logout.

நோட்:
- TOTP (Google Authenticator) required for login.
- Session refresh happens automatically on token expiry.
- Falls back to NSE scraping if Angel One credentials are not configured.

FIXES (2026-08-20):
  - get_option_chain(): use_sdk check-க்கு INFO log சேர்த்தோம் —
    "use_sdk=False" வந்தா SmartAPI SDK upgrade தேவை என்று தெரியும்.
  - REST fallback-ல் WARNING log சேர்த்தோம் — "Invalid Token" error
    silent-ஆ fail ஆகாமல் logs-ல் தெரியும்.
"""

import asyncio
from contextvars import ContextVar
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, Dict, List
import httpx
from app.config import settings
from app.utils.helpers import safe_float, epoch_to_ist, now_ist
from app.services.options_greeks import implied_volatility_from_price
from app.utils.helpers import days_to_expiry

logger = logging.getLogger(__name__)


def _normalize_expiry(e: str) -> str:
    """Expiry string-ஐ canonical date format (YYYY-MM-DD) ஆக மாற்றுகிறது.

    Angel One instrument master: '01SEP2026' format.
    UI / API caller: '01-Sep-2026' அல்லது '2026-09-01' format.
    இந்த mismatch-ஐ தடுக்க — compare செய்வதற்கு முன்பு
    இரண்டையும் ஒரே canonical format-க்கு convert செய்கிறோம்.

    Returns the original string unchanged if parsing fails
    (so callers still see something useful in logs).
    """
    if not e:
        return e
    e2 = e.strip()
    m = re.match(r"(\d{1,2})[-/]?([A-Za-z]{3})[-/]?(\d{4})", e2)
    if m:
        try:
            dt = datetime.strptime(
                f"{m.group(1)}{m.group(2).title()}{m.group(3)}", "%d%b%Y"
            )
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(e2, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return e  # unparseable — return as-is


class AngelOneError(Exception):
    pass


class AngelOneAuthError(AngelOneError):
    pass


# Angel One's SmartAPI (especially the historical candle endpoint) rejects
# rapid-fire requests with "Access denied because of exceeding access rate"
# once two or more calls land within roughly the same second. dashboard.py's
# /api/dashboard/summary fetches NIFTY, BANKNIFTY, and FINNIFTY concurrently
# via asyncio.gather(), and each of those independently calls into Angel One
# (get_candle_data, get_ltp, get_option_chain's quote batches) — so without
# serialization, every dashboard load fires 2-3 SmartAPI calls at once and
# Angel One throttles at least one of them. A minimum gap between calls,
# shared across the whole process via one lock, fixes this without needing
# to change how dashboard.py fetches symbols.
# Priority-aware broker scheduling.
# HIGH       = live user-facing actions such as positions / EXIT
# NORMAL     = browser strategy / AI requests
# BACKGROUND = historical collector
ANGEL_PRIORITY_HIGH = "high"
ANGEL_PRIORITY_NORMAL = "normal"
ANGEL_PRIORITY_BACKGROUND = "background"

_angel_call_priority: ContextVar[str] = ContextVar(
    "angel_call_priority",
    default=ANGEL_PRIORITY_NORMAL,
)

def set_angel_call_priority(priority: str):
    if priority not in (
        ANGEL_PRIORITY_HIGH,
        ANGEL_PRIORITY_NORMAL,
        ANGEL_PRIORITY_BACKGROUND,
    ):
        raise ValueError(f"Unknown Angel One priority: {priority}")
    return _angel_call_priority.set(priority)

def reset_angel_call_priority(token) -> None:
    _angel_call_priority.reset(token)


def get_angel_call_priority() -> str:
    """Return the current Angel One broker-call priority."""
    return _angel_call_priority.get()


_MIN_CALL_INTERVAL = 1.2  # seconds between any two Angel One SmartAPI calls


class AngelOneSession:
    """
    Angel One SmartAPI session wrapper.
    Login, token refresh, and logout handle ஆகும்.
    """

    def __init__(self):
        self._obj = None
        self._auth_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._logged_in: bool = False
        self._login_ts: float = 0.0
        self._lock = asyncio.Lock()
        # Session valid for ~8 hours; refresh 30 min before expiry
        self._session_ttl: int = 7 * 3600
        # Instrument master cache (see _ensure_instruments below)
        self._instruments: Optional[List[Dict]] = None
        self._instruments_ts: float = 0.0
        self._instruments_lock = asyncio.Lock()
        # Rate limiter shared by every SmartAPI call this session makes —
        # see _MIN_CALL_INTERVAL comment above for why this exists.
        self._rate_lock = asyncio.Lock()
        self._last_call_ts: float = 0.0

        # Waiting broker calls are ordered by priority, then FIFO.
        # The actual SmartAPI call is still performed outside this gate.
        self._priority_waiters: dict[int, tuple[int, str]] = {}
        self._priority_seq: int = 0
        self._priority_condition = asyncio.Condition(self._rate_lock)
        # BUG FIX (restored — was dropped without explanation and is not
        # covered by any comment elsewhere in this file, unlike every other
        # intentional behavior change here): when one concurrent call hits
        # Angel One's "exceeding access rate" error, every OTHER in-flight
        # call sharing this session needs to know about it too, or they will
        # walk straight into the same rate limit a few hundred ms later
        # (asyncio.gather() fan-out is exactly the case _throttle()'s own
        # docstring says this class exists to protect). Without this shared
        # cooldown, only the one call that failed backs off — the rest just
        # see _last_call_ts and think they're clear.
        self._rate_limit_cooldown_until: float = 0.0

        # HISTORICAL_API_SERIALIZATION_20260924
        # getCandleData has its own broker-side rate limit. The global
        # _throttle() spaces call START times but deliberately releases its
        # gate before the blocking SDK request finishes. Therefore two
        # historical requests can still be in-flight simultaneously.
        # Serialize only historical SDK calls; keep other SmartAPI paths
        # independent.
        self._historical_api_lock = asyncio.Lock()

    async def _throttle(
        self,
        priority: str | None = None,
        call_name: str = "unknown",
    ) -> None:
        """
        Global Angel One rate gate with priority scheduling.

        Keeps:
          - 1.2s minimum spacing
          - global rate-limit cooldown
          - actual SmartAPI call outside the gate

        Priority:
          high       -> live user action
          normal     -> browser strategy/AI
          background -> history collector
        """
        if priority is None:
            priority = _angel_call_priority.get()

        rank = {
            ANGEL_PRIORITY_HIGH: 0,
            ANGEL_PRIORITY_NORMAL: 1,
            ANGEL_PRIORITY_BACKGROUND: 2,
        }.get(priority)

        if rank is None:
            raise ValueError(f"Unknown Angel One priority: {priority}")

        async with self._priority_condition:
            self._priority_seq += 1
            seq = self._priority_seq
            self._priority_waiters[seq] = (rank, priority)

            wait_started = time.perf_counter()

            try:
                while True:
                    best_seq = min(
                        self._priority_waiters,
                        key=lambda s: (
                            self._priority_waiters[s][0],
                            s,
                        ),
                    )

                    now = time.time()

                    interval_wait = max(
                        0.0,
                        _MIN_CALL_INTERVAL -
                        (now - self._last_call_ts),
                    )

                    cooldown_wait = max(
                        0.0,
                        self._rate_limit_cooldown_until - now,
                    )

                    wait = max(interval_wait, cooldown_wait)

                    if best_seq == seq and wait <= 0:
                        self._priority_waiters.pop(seq, None)
                        self._last_call_ts = time.time()
                        self._priority_condition.notify_all()
                        break

                    try:
                        await asyncio.wait_for(
                            self._priority_condition.wait(),
                            timeout=max(wait, 0.05),
                        )
                    except asyncio.TimeoutError:
                        pass

            except BaseException:
                self._priority_waiters.pop(seq, None)
                self._priority_condition.notify_all()
                raise

            waited = time.perf_counter() - wait_started

            if waited >= 0.5:
                logger.info(
                    "Angel throttle call=%s priority=%s waited=%.3fs queued=%d",
                    call_name,
                    priority,
                    waited,
                    len(self._priority_waiters),
                )

    @property
    def is_configured(self) -> bool:
        return bool(
            getattr(settings, "angel_api_key", None)
            and getattr(settings, "angel_client_id", None)
            and getattr(settings, "angel_password", None)
            and getattr(settings, "angel_totp_secret", None)
        )

    @property
    def is_logged_in(self) -> bool:
        if not self._logged_in or not self._auth_token:
            return False
        # Check session age
        return (time.time() - self._login_ts) < self._session_ttl

    def _get_totp(self) -> str:
        import pyotp
        return pyotp.TOTP(settings.angel_totp_secret).now()

    async def _login_locked(self) -> Dict:
        """Perform Angel One login while self._lock is already held."""
        if not self.is_configured:
            raise AngelOneAuthError(
                "Angel One credentials not configured. "
                ".env-ல் ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP_SECRET சேர்க்கவும்."
            )

        if self.is_logged_in:
            return {
                "status": "already_logged_in",
                "client_id": settings.angel_client_id,
            }

        try:
            from SmartApi import SmartConnect
        except ImportError:
            raise AngelOneError(
                "smartapi-python package இல்லை. "
                "`pip install smartapi-python pyotp` run செய்யவும்."
            )

        try:
            self._obj = SmartConnect(api_key=settings.angel_api_key)
            totp = self._get_totp()

            # generateSession() is blocking; keep it off the event loop.
            data = await asyncio.to_thread(
                self._obj.generateSession,
                settings.angel_client_id,
                settings.angel_password,
                totp,
            )
        except Exception as e:
            raise AngelOneAuthError(f"Angel One login failed: {e}")

        if not data or data.get("status") is False:
            msg = data.get("message", "Unknown error") if data else "No response"
            raise AngelOneAuthError(f"Angel One login error: {msg}")

        tokens = data.get("data", {})
        self._auth_token = tokens.get("jwtToken") or tokens.get("accessToken")
        self._refresh_token = tokens.get("refreshToken")
        self._feed_token = await asyncio.to_thread(self._obj.getfeedToken)
        self._logged_in = True
        self._login_ts = time.time()

        logger.info(
            f"Angel One login successful — client: {settings.angel_client_id}"
        )

        return {
            "status": "success",
            "client_id": settings.angel_client_id,
            "feed_token": self._feed_token,
            "session_expiry": epoch_to_ist(
                  self._login_ts + self._session_ttl
              ).strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def login(self) -> Dict:
        """Angel One-ல் login செய்து auth/feed tokens return செய்யும்."""
        async with self._lock:
            return await self._login_locked()

    async def logout(self) -> Dict:
        """Session logout செய்யும்."""
        async with self._lock:
            if not self._logged_in or not self._obj:
                return {"status": "not_logged_in"}
            try:
                # FIX (event-loop block): run in a thread, same as login.
                resp = await asyncio.to_thread(
                    self._obj.terminateSession, settings.angel_client_id
                )
                logger.info("Angel One logout successful")
            except Exception as e:
                logger.warning(f"Angel One logout error (ignored): {e}")
            finally:
                self._logged_in = False
                self._auth_token = None
                self._refresh_token = None
                self._feed_token = None
                self._obj = None
            return {"status": "logged_out"}

    async def ensure_session(self):
        """Not logged in-ஆனால் auto-login செய்யும்."""
        if not self.is_logged_in:
            await self.login()

    def _invalidate_session_locked(self) -> None:
        """Clear the Angel One session while self._lock is held."""
        self._logged_in = False
        self._auth_token = None
        self._refresh_token = None
        self._feed_token = None
        self._obj = None

    @staticmethod
    def _is_invalid_token(value) -> bool:
        """Return True for Angel One broker-side invalid-token responses."""
        text = str(value).lower()
        return (
            "ag8001" in text
            or "invalid token" in text
        )

    async def _recover_invalid_token(self, failed_obj=None) -> bool:
        """
        Recover one stale/invalid Angel One session safely.

        If another coroutine has already replaced the failed session object,
        keep that newer session instead of clearing it.
        """
        async with self._lock:
            if (
                failed_obj is not None
                and self._obj is not failed_obj
                and self.is_logged_in
            ):
                logger.info(
                    "Angel One session already recovered by another coroutine"
                )
                return True

            self._invalidate_session_locked()

            try:
                await self._login_locked()
                logger.info(
                    "Angel One session recovered after Invalid Token"
                )
                return True
            except Exception as e:
                logger.warning(
                    "Angel One session recovery failed: %s",
                    e,
                )
                return False

    def get_status(self) -> Dict:
        """தற்போதைய session status return செய்யும்."""
        configured = self.is_configured
        return {
            "configured": configured,
            "logged_in": self.is_logged_in,
            "client_id": getattr(settings, "angel_client_id", None) if configured else None,
            "session_age_minutes": round((time.time() - self._login_ts) / 60, 1) if self._logged_in else 0,
        }

    # ─── Market Data Methods ───────────────────────────────────────────────

    # Symbol tokens for Angel One index spot instruments.
    # NIFTY/BANKNIFTY/FINNIFTY remain NSE; SENSEX is BSE.
    SYMBOL_TOKENS = {
        "NIFTY":     {"token": "99926000", "exchange": "NSE"},
        "BANKNIFTY": {"token": "99926009", "exchange": "NSE"},
        "FINNIFTY":  {"token": "99926037", "exchange": "NSE"},
        "SENSEX":    {"token": "99919000", "exchange": "BSE"},
    }

    # Index option/future instrument-master name map.
    NFO_SYMBOL_MAP = {
        "NIFTY":     "NIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "FINNIFTY":  "FINNIFTY",
        "SENSEX":    "SENSEX",
    }

    # Angel instrument-master derivatives segment by index.
    # Existing NSE/NFO symbols remain unchanged; SENSEX uses BFO.
    DERIVATIVE_EXCHANGE_MAP = {
        "NIFTY": "NFO",
        "BANKNIFTY": "NFO",
        "FINNIFTY": "NFO",
        "SENSEX": "BFO",
    }

    async def get_ltp(self, symbol: str) -> Dict:
        """Live LTP fetch — Angel One SmartAPI with one invalid-token recovery."""
        await self.ensure_session()
        info = self.SYMBOL_TOKENS.get(symbol.upper())
        if not info:
            raise AngelOneError(f"Unknown symbol: {symbol}")

        symbol_upper = symbol.upper()

        async def _call_ltp():
            await self._throttle(call_name=f"get_ltp:{symbol_upper}")
            return await asyncio.to_thread(
                self._obj.ltpData,
                info["exchange"],
                symbol_upper,
                info["token"],
            )

        # Keep the session object that made the request. If another
        # coroutine already recovered the session, recovery will reuse it.
        failed_obj = self._obj

        try:
            data = await _call_ltp()
        except Exception as e:
            if self._is_invalid_token(e):
                logger.warning(
                    "Angel One Invalid Token on get_ltp:%s; attempting recovery",
                    symbol_upper,
                )

                if not await self._recover_invalid_token(failed_obj=failed_obj):
                    raise AngelOneError(
                        f"LTP fetch failed for {symbol}: Invalid Token "
                        f"and session recovery failed"
                    )

                # Retry exactly once after successful recovery.
                try:
                    data = await _call_ltp()
                except Exception as retry_error:
                    raise AngelOneError(
                        f"LTP fetch failed after session recovery for "
                        f"{symbol}: {retry_error}"
                    )

            else:
                raise AngelOneError(f"LTP fetch failed for {symbol}: {e}")

        # SmartApi may return a plain string for some HTTP/API errors
        # (for example: "Invalid Token").  Never call .get() on a
        # non-dict response.
        if isinstance(data, str):
            if self._is_invalid_token(data):
                logger.warning(
                    "Angel One Invalid Token string response on get_ltp:%s; "
                    "attempting recovery",
                    symbol_upper,
                )

                if not await self._recover_invalid_token(
                    failed_obj=failed_obj
                ):
                    raise AngelOneError(
                        f"LTP error for {symbol}: Invalid Token "
                        f"and session recovery failed"
                    )

                try:
                    data = await _call_ltp()
                except Exception as retry_error:
                    raise AngelOneError(
                        f"LTP fetch failed after session recovery for "
                        f"{symbol}: {retry_error}"
                    )

                if isinstance(data, str):
                    if self._is_invalid_token(data):
                        raise AngelOneError(
                            f"LTP error after session recovery for "
                            f"{symbol}: Invalid Token"
                        )
                    raise AngelOneError(
                        f"LTP error after session recovery for "
                        f"{symbol}: {data}"
                    )

            else:
                raise AngelOneError(
                    f"LTP error for {symbol}: {data}"
                )

        if not isinstance(data, dict):
            raise AngelOneError(
                f"LTP error for {symbol}: unexpected response type "
                f"{type(data).__name__}"
            )

        if not data or data.get("status") is False or data.get("success") is False:
            message = data.get("message", "Unknown") if data else "No response"

            if self._is_invalid_token(data):
                logger.warning(
                    "Angel One Invalid Token response on get_ltp:%s; "
                    "attempting recovery",
                    symbol_upper,
                )

                if not await self._recover_invalid_token(
                    failed_obj=failed_obj
                ):
                    raise AngelOneError(
                        f"LTP error for {symbol}: Invalid Token "
                        f"and session recovery failed"
                    )

                # Retry exactly once after broker-side invalid-token response.
                try:
                    data = await _call_ltp()
                except Exception as retry_error:
                    raise AngelOneError(
                        f"LTP fetch failed after session recovery for "
                        f"{symbol}: {retry_error}"
                    )

                if not data or data.get("status") is False or data.get("success") is False:
                    retry_message = (
                        data.get("message", "Unknown")
                        if data
                        else "No response"
                    )
                    raise AngelOneError(
                        f"LTP error after session recovery for "
                        f"{symbol}: {retry_message}"
                    )
            else:
                raise AngelOneError(f"LTP error: {message}")

        ltp_data = data.get("data", {})
        return {
            "symbol": symbol.upper(),
            "price": float(ltp_data.get("ltp", 0)),
            "open": float(ltp_data.get("open", 0)),
            "high": float(ltp_data.get("high", 0)),
            "low": float(ltp_data.get("low", 0)),
            "close": float(ltp_data.get("close", 0)),
            "change": float(ltp_data.get("ltp", 0)) - float(ltp_data.get("close", 1)),
            "change_percent": round(
                (float(ltp_data.get("ltp", 0)) - float(ltp_data.get("close", 1)))
                / max(float(ltp_data.get("close", 1)), 1) * 100, 2
            ),
            "source": "angel_one",
            "timestamp": now_ist().isoformat()
        }

    # India VIX token — kept separate from SYMBOL_TOKENS/get_ltp() above
    # because Angel's official tradingsymbol for this instrument is
    # "India VIX" (with a space), not a plain "INDIAVIX" — reusing the
    # generic get_ltp(symbol) path would silently send the wrong
    # tradingsymbol string. Isolating it here means the existing
    # NIFTY/BANKNIFTY/FINNIFTY path above is completely untouched.
    INDIA_VIX_TOKEN = "99926017"

    async def get_india_vix(self) -> float:
        """India VIX LTP via Angel One with one invalid-token recovery."""
        await self.ensure_session()

        async def _call_vix():
            await self._throttle(call_name="get_india_vix")
            return await asyncio.to_thread(
                self._obj.ltpData,
                "NSE",
                "India VIX",
                self.INDIA_VIX_TOKEN,
            )

        # Keep the session object that made the first request.
        failed_obj = self._obj

        try:
            data = await _call_vix()
        except Exception as e:
            if self._is_invalid_token(e):
                logger.warning(
                    "Angel One Invalid Token on get_india_vix; attempting recovery"
                )

                if not await self._recover_invalid_token(
                    failed_obj=failed_obj
                ):
                    raise AngelOneError(
                        "India VIX fetch failed: Invalid Token "
                        "and session recovery failed"
                    )

                # Retry exactly once after recovery.
                try:
                    data = await _call_vix()
                except Exception as retry_error:
                    raise AngelOneError(
                        "India VIX fetch failed after session recovery: "
                        f"{retry_error}"
                    )
            else:
                raise AngelOneError(
                    f"India VIX LTP fetch failed: {e}"
                )

        if not data or data.get("status") is False:
            message = (
                data.get("message", "Unknown")
                if data
                else "No response"
            )

            if self._is_invalid_token(data):
                logger.warning(
                    "Angel One Invalid Token response on get_india_vix; "
                    "attempting recovery"
                )

                if not await self._recover_invalid_token(
                    failed_obj=failed_obj
                ):
                    raise AngelOneError(
                        "India VIX LTP error: Invalid Token "
                        "and session recovery failed"
                    )

                # Retry exactly once after broker-side invalid-token response.
                try:
                    data = await _call_vix()
                except Exception as retry_error:
                    raise AngelOneError(
                        "India VIX fetch failed after session recovery: "
                        f"{retry_error}"
                    )

                if not data or data.get("status") is False:
                    retry_message = (
                        data.get("message", "Unknown")
                        if data
                        else "No response"
                    )
                    raise AngelOneError(
                        "India VIX LTP error after session recovery: "
                        f"{retry_message}"
                    )
            else:
                raise AngelOneError(
                    f"India VIX LTP error: {message}"
                )

        val = float(
            (data.get("data") or {}).get("ltp", 0) or 0
        )

        if val <= 0:
            raise AngelOneError("India VIX LTP returned 0")

        return val

    # ── Instrument master (needed to resolve option strike → token) ─────────

    INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    QUOTE_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote"

    # BUG FIX (2026-08-22): referenced by _download_instrument_master_chunked/
    # _download_instrument_master_whole below but never actually defined —
    # calling either of those would have raised
    # AttributeError: 'AngelOneSession' object has no attribute
    # '_INSTRUMENT_MASTER_HEADERS'. This is almost certainly *why*
    # _ensure_instruments() below was short-circuited to always raise
    # instead of calling them — a plain User-Agent is enough for this
    # static-file CDN (it isn't behind the same bot-detection as NSE).
    _INSTRUMENT_MASTER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    # Instrument master is published once per trading day — cache well
    # under that so a whole trading session reuses one download.
    _INSTRUMENT_MASTER_TTL = 24 * 3600  # hard upper bound; actual refresh is calendar-day based

    # Persistent cache contains ONLY the filtered NFO rows actually needed by
    # this application (~3k rows), not the 30-40MB raw Angel master.
    # This prevents every service restart from re-downloading the full master.
    _INSTRUMENT_CACHE_FILE = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "angel_instrument_master_filtered.json"
    )

    async def _ensure_instruments(self) -> None:
        """
        Ensure the filtered Angel One instrument master is available.

        Cache layers:
          1. in-memory cache for normal requests;
          2. same-day persistent disk cache across service restarts;
          3. Angel One download only when the daily disk cache is missing,
             stale, corrupt, or invalid.

        The persistent cache stores only NIFTY/BANKNIFTY/FINNIFTY NFO
        options/futures rows, so it remains small and avoids retaining the
        full 150k+ raw instrument master in memory.
        """
        now = time.time()
        today = now_ist().date()
        cache_day = getattr(self, "_instruments_cache_day", None)

        # Fast path: existing in-memory same-day cache.
        if (
            isinstance(self._instruments, list)
            and self._instruments
            and cache_day == today
            and (now - self._instruments_ts) < self._INSTRUMENT_MASTER_TTL
        ):
            return

        async with self._instruments_lock:
            # Re-check after acquiring the lock.
            now = time.time()
            today = now_ist().date()
            cache_day = getattr(self, "_instruments_cache_day", None)

            if (
                isinstance(self._instruments, list)
                and self._instruments
                and cache_day == today
                and (now - self._instruments_ts) < self._INSTRUMENT_MASTER_TTL
            ):
                return

            # --------------------------------------------------------------
            # Persistent same-day cache.
            # --------------------------------------------------------------
            cache_file = self._INSTRUMENT_CACHE_FILE

            try:
                if cache_file.is_file():
                    cached = await asyncio.to_thread(
                        self._load_persistent_instrument_cache,
                        cache_file,
                        today,
                    )

                    if cached:
                        self._instruments = cached
                        self._instruments_ts = time.time()
                        self._instruments_cache_day = today

                        logger.info(
                            "Angel One instrument master loaded from disk cache "
                            "— %d rows, cache_day=%s",
                            len(cached),
                            today,
                        )
                        return
            except Exception as e:
                # A bad cache must never prevent a fresh Angel master download.
                logger.warning(
                    "Angel One instrument disk cache unavailable; "
                    "refreshing master: %s",
                    e,
                )

            # --------------------------------------------------------------
            # No valid same-day disk cache: download the raw master.
            # --------------------------------------------------------------
            try:
                loaded = await self._download_instrument_master_chunked()
            except Exception as e_chunked:
                logger.warning(
                    f"Instrument master chunked download failed, "
                    f"falling back to whole-file download: {e_chunked}"
                )
                try:
                    loaded = await self._download_instrument_master_whole()
                except Exception as e_whole:
                    raise AngelOneError(
                        f"Instrument master download failed "
                        f"(chunked: {e_chunked}; whole: {e_whole})"
                    )

            if not isinstance(loaded, list) or not loaded:
                raise AngelOneError("Instrument master download returned no rows")

            # Keep only the instruments this application actually resolves.
            _wanted_names = set(self.NFO_SYMBOL_MAP.values())
            _wanted_exchanges = set(self.DERIVATIVE_EXCHANGE_MAP.values())

            def _filter(rows):
                return [
                    r for r in rows
                    if isinstance(r, dict)
                    and r.get("exch_seg") in _wanted_exchanges
                    and r.get("name") in _wanted_names
                    and r.get("instrumenttype") in ("OPTIDX", "FUTIDX")
                    and r.get("expiry")
                ]

            # Offload the 150k-row filter from the event loop.
            loaded = await asyncio.to_thread(_filter, loaded)

            if not loaded:
                raise AngelOneError(
                    "Instrument master download returned no matching "
                    "NIFTY/BANKNIFTY/FINNIFTY rows after filtering"
                )

            self._instruments = loaded
            self._instruments_ts = time.time()
            self._instruments_cache_day = today

            # Persist ONLY the filtered rows. Failure to write the cache must
            # not make an otherwise successful broker download fail.
            try:
                await asyncio.to_thread(
                    self._save_persistent_instrument_cache,
                    cache_file,
                    today,
                    loaded,
                )
            except Exception as e:
                logger.warning(
                    "Angel One instrument disk cache write failed "
                    "(memory cache remains valid): %s",
                    e,
                )

            logger.info(
                "Angel One instrument master cached — %d rows "
                "(filtered to NIFTY/BANKNIFTY/FINNIFTY), "
                "cache_day=%s",
                len(loaded),
                today,
            )

    @staticmethod
    def _load_persistent_instrument_cache(
        cache_file: Path,
        today,
    ) -> Optional[List[Dict]]:
        """Load and validate the small same-day filtered instrument cache."""
        with cache_file.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        if not isinstance(payload, dict):
            return None

        cached_day = payload.get("cache_day")
        rows = payload.get("rows")

        if cached_day != today.isoformat():
            return None

        if not isinstance(rows, list) or not rows:
            return None

        # Validate the shape enough to avoid treating arbitrary/stale JSON
        # as a valid instrument master.
        valid_rows = [
            r for r in rows
            if (
                isinstance(r, dict)
                and r.get("exch_seg") in ("NFO", "BFO")
                and r.get("instrumenttype") in ("OPTIDX", "FUTIDX")
                and r.get("expiry")
                and r.get("name")
                and r.get("token")
            )
        ]


        # Reject an older same-day cache that contains only NFO rows.
        # The current instrument schema must include real SENSEX BFO
        # derivatives so SENSEX can resolve without NSE/NFO substitution.
        has_sensex_bfo = any(
            isinstance(r, dict)
            and r.get("name") == "SENSEX"
            and r.get("exch_seg") == "BFO"
            and r.get("instrumenttype") in ("OPTIDX", "FUTIDX")
            and r.get("expiry")
            and r.get("token")
            for r in valid_rows
        )

        if not has_sensex_bfo:
            return None
        return valid_rows or None

    @staticmethod
    def _save_persistent_instrument_cache(
        cache_file: Path,
        today,
        rows: List[Dict],
    ) -> None:
        """Atomically save the filtered daily instrument cache."""
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "cache_day": today.isoformat(),
            "rows": rows,
        }

        tmp_file = cache_file.with_name(
            cache_file.name + ".tmp"
        )

        with tmp_file.open("w", encoding="utf-8") as f:
            json.dump(
                payload,
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )

        tmp_file.replace(cache_file)

    async def _download_instrument_master_chunked(self):
        """
        Downloads OpenAPIScripMaster.json in ~4MB Range-request chunks
        instead of one ~37MB request. See the FIX note in _ensure_instruments
        for why: the server appears to cut off long-lived connections to
        this file after a fixed duration, and a 4MB chunk finishes well
        inside that window even at the slow throughput observed in prod
        (~60-150 KB/s from Render to this host).

        Raises (never returns a partial/invalid result) if the server
        doesn't support Range requests, or if any chunk fails after its own
        retries — the caller falls back to _download_instrument_master_whole
        in that case.
        """
        CHUNK = 4 * 1024 * 1024  # 4MB
        async with httpx.AsyncClient(timeout=30, headers=self._INSTRUMENT_MASTER_HEADERS) as client:
            # Probe: ask for just the first byte range. If the server
            # answers 206 with a Content-Range header, it supports ranged
            # requests and tells us the true total size.
            probe = await client.get(
                self.INSTRUMENT_MASTER_URL, headers={"Range": "bytes=0-0"}
            )
            if probe.status_code != 206:
                raise AngelOneError(
                    f"Server does not support Range requests (probe status {probe.status_code})"
                )
            content_range = probe.headers.get("Content-Range", "")
            # Format: "bytes 0-0/36952921"
            total_size = None
            if "/" in content_range:
                try:
                    total_size = int(content_range.rsplit("/", 1)[-1])
                except ValueError:
                    pass
            if not total_size:
                raise AngelOneError(f"Could not parse total size from Content-Range: {content_range!r}")

            buf = bytearray()
            pos = 0
            while pos < total_size:
                end = min(pos + CHUNK, total_size) - 1
                chunk_bytes = None
                for attempt in range(3):
                    try:
                        r = await client.get(
                            self.INSTRUMENT_MASTER_URL,
                            headers={"Range": f"bytes={pos}-{end}"},
                        )
                        if r.status_code not in (200, 206):
                            raise AngelOneError(f"Chunk fetch got HTTP {r.status_code}")
                        chunk_bytes = r.content
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise AngelOneError(
                                f"Chunk [{pos}-{end}] failed after 3 attempts — "
                                f"{type(e).__name__}: {e or '(no message)'}"
                            )
                        await asyncio.sleep(1.0 * (attempt + 1))
                buf.extend(chunk_bytes)
                pos = end + 1
            logger.info(
                f"Angel One instrument master downloaded via {(total_size // CHUNK) + 1} "
                f"Range chunks — {len(buf)} bytes"
            )
            # FIX (health-check timeouts): json.loads() on a ~35MB payload is
            # pure-Python CPU work — it was running directly on the event
            # loop thread, so for however long parsing took (worse under
            # Render's throttled free-tier CPU), the loop couldn't answer
            # ANY other request, including the "/" health check. Render logs
            # this as "HTTP health check failed (timed out after 5 seconds)"
            # — confirmed against Render's Events tab, which shows failures
            # at the exact UTC-vs-IST-adjusted timestamps this was hit.
            # Offloading to a thread lets the loop keep serving the health
            # check (and other requests) while the parse runs.
            return await asyncio.to_thread(json.loads, buf)

    async def _download_instrument_master_whole(self):
        """
        Fallback used when the server doesn't honor Range requests (some
        CDNs don't) — same streaming-with-retries approach as round 2, kept
        as a safety net rather than the primary path now.
        """
        last_err: Optional[Exception] = None
        loaded = None
        for attempt in range(4):
            try:
                chunks = bytearray()
                async with httpx.AsyncClient(timeout=90, headers=self._INSTRUMENT_MASTER_HEADERS) as client:
                    async with client.stream("GET", self.INSTRUMENT_MASTER_URL) as resp:
                        resp.raise_for_status()
                        async for chunk in resp.aiter_bytes():
                            chunks.extend(chunk)
                loaded = await asyncio.to_thread(json.loads, chunks)
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    f"Angel One instrument master whole-file download attempt {attempt + 1}/4 "
                    f"failed — {type(e).__name__}: {e or '(no message — likely a timeout)'} "
                    f"({len(chunks)} bytes received before failure)"
                )
                loaded = None
                if attempt < 3:
                    await asyncio.sleep(2 * (attempt + 1))
        if loaded is None:
            raise AngelOneError(
                f"Instrument master whole-file download failed after 4 attempts — "
                f"{type(last_err).__name__}: {last_err or '(no message — likely a timeout)'}"
            )
        return loaded

    async def warmup_instruments(self) -> None:
        """
        Pre-fetches and caches the instrument master at startup so the
        first real option-chain/futures request doesn't pay the download
        cost inline. BUG FIX (2026-08-22): this used to be a no-op
        ("Render-safe: skip startup warmup") to match _ensure_instruments()
        being permanently disabled — now that _ensure_instruments() does
        the real (chunked, Render-safe) download, warmup should actually
        run it. Failure here is non-fatal — it's just a head start; the
        first real caller will retry via _ensure_instruments() anyway.
        """
        try:
            await self._ensure_instruments()
        except Exception as e:
            logger.warning(f"Instrument warmup failed (will retry on first use): {e}")

    @staticmethod
    def _expiry_sort_key(e: str):
        for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(e, fmt)
            except ValueError:
                continue
        return datetime.max

    async def get_option_chain(
        self,
        symbol: str,
        expiry: Optional[str] = None,
        strikes_each_side: Optional[int] = 10,
        spot_price: Optional[float] = None,
    ) -> Dict:
        """
        Composes an option chain for `symbol` from the instrument master +
        live quotes. Angel One has no single "get option chain" endpoint
        the way NSE does — this resolves CE/PE strike tokens near spot for
        the requested (or nearest) expiry from the instrument master, then
        batches live quotes for those tokens.

        NOTE (2026-08-22): _ensure_instruments() now actually downloads and
        caches the instrument master (see its docstring for the bug that
        used to disable this) instead of always raising. It can still
        raise AngelOneError if the download genuinely fails — callers
        (data_fetcher) already catch that and fall back to NSE.

        FIX (2026-08-21): this method previously had no `def` line — its
        body had been left dangling inside warmup_instruments() after an
        early `return`, so it was dead code and the method didn't exist on
        the class at all ('AngelOneSession' object has no attribute
        'get_option_chain'). Restored as its own method here.
        """
        await self.ensure_session()
        await self._ensure_instruments()
        if not isinstance(self._instruments, list) or not self._instruments:
            raise AngelOneError("Instrument master not available")

        sym = self.NFO_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        derivative_exchange = self.DERIVATIVE_EXCHANGE_MAP.get(
            symbol.upper(), "NFO"
        )
        rows = [
            r for r in self._instruments
            if isinstance(r, dict)
            and r.get("name") == sym
            and r.get("instrumenttype") == "OPTIDX"
            and r.get("exch_seg") == derivative_exchange
            and r.get("expiry")
        ]
        if not rows:
            raise AngelOneError(f"No option instruments found for {sym}")

        expiries = sorted({r["expiry"] for r in rows}, key=self._expiry_sort_key)

        # '01SEP2026' while the UI/caller sends '01-Sep-2026' or '2026-09-01'.
        # Normalize BOTH sides to 'YYYY-MM-DD' before comparing.
        #
        # Important:
        # Angel's instrument master can retain expired contracts at the front
        # of the sorted expiry list. Never use expiries[0] blindly because an
        # expired contract can make getMarketData return AB4030 / zero quotes.

        from datetime import date

        today = date.today()

        valid_expiries = []
        for e in expiries:
            try:
                e_norm = _normalize_expiry(e)
                if not e_norm:
                    continue
                e_date = date.fromisoformat(e_norm)
                if e_date >= today:
                    valid_expiries.append(e)
            except Exception:
                logger.debug(
                    "Angel One: unable to parse expiry %r while filtering expired contracts",
                    e,
                )

        if not valid_expiries:
            raise AngelOneError(
                f"No current/future option expiries found for {sym}"
            )

        if expiry:
            norm_requested = _normalize_expiry(expiry)

            # Exact normalized expiry match, but only if it is current/future.
            matched = next(
                (
                    e for e in valid_expiries
                    if _normalize_expiry(e) == norm_requested
                ),
                None,
            )

            if matched:
                chosen_expiry = matched
            else:
                logger.warning(
                    "Requested expiry %r (normalized: %s) is expired/unavailable "
                    "— falling back to nearest valid expiry %r",
                    expiry,
                    norm_requested,
                    valid_expiries[0],
                )
                chosen_expiry = valid_expiries[0]
        else:
            # No expiry supplied: always use nearest current/future expiry.
            chosen_expiry = valid_expiries[0]

        logger.info(
            "Angel One option chain expiry selected: %s "
            "(requested=%r, valid_expiries=%s)",
            chosen_expiry,
            expiry,
            valid_expiries[:5],
        )

        rows = [r for r in rows if r.get("expiry") == chosen_expiry]
        if not rows:
            raise AngelOneError(f"No option rows for {sym} expiry {chosen_expiry}")

        if spot_price is not None and float(spot_price) > 0:
            spot = float(spot_price)
            logger.debug(
                "Angel One option chain: reusing supplied spot=%s for %s",
                spot,
                symbol,
            )
        else:
            spot_info = await self.get_ltp(symbol)
            spot = spot_info["price"]

        def _strike(r: Dict) -> float:
            # Angel stores strike as price * 100, e.g. "2450000.000000"
            try:
                return float(r.get("strike", 0)) / 100.0
            except (TypeError, ValueError):
                return 0.0

        all_strikes = sorted({_strike(r) for r in rows if _strike(r) > 0})
        if not all_strikes:
            raise AngelOneError(f"No valid strikes parsed for {sym} {chosen_expiry}")

        atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - spot))
        if strikes_each_side is None:
            selected_strikes = set(all_strikes)
        else:
            lo = max(0, atm_idx - strikes_each_side)
            hi = min(len(all_strikes), atm_idx + strikes_each_side + 1)
            selected_strikes = set(all_strikes[lo:hi])

        selected_rows = [r for r in rows if _strike(r) in selected_strikes]
        tokens = [str(r["token"]) for r in selected_rows if r.get("token")]  # FIX: str() normalize
        if not tokens:
            raise AngelOneError(f"No tokens resolved for {sym} {chosen_expiry}")

        futures_token = None
        futures_info = None
        try:
            futures_info = await self._resolve_futures_token(sym)
            if futures_info and futures_info.get("token") and len(tokens) < 50:
                candidate_futures_token = str(futures_info["token"])
                if candidate_futures_token not in tokens:
                    tokens.append(candidate_futures_token)
                futures_token = candidate_futures_token
        except Exception as e:
            logger.debug("Bundled futures token unavailable for %s: %s", sym, e)

        # FIX (2026-08-20): use_sdk check-க்கு INFO log சேர்த்தோம்.
        # "use_sdk=False" வந்தா SmartAPI SDK-ல் getMarketData இல்லை —
        # REST fallback try ஆகும், அது "Invalid Token" error குடுக்கும்.
        # Solution: pip install --upgrade smartapi-python
        use_sdk = hasattr(self._obj, "getMarketData")
        logger.info(
            f"Angel One option chain: {sym} expiry={chosen_expiry}, "
            f"tokens={len(tokens)}, spot={spot}, use_sdk={use_sdk}"
        )
        if not use_sdk:
            logger.warning(
                "Angel One SDK does not have getMarketData method — "
                "will try REST fallback which may fail with 'Invalid Token'. "
                "Fix: pip install --upgrade smartapi-python (in requirements.txt)"
            )

        quotes: Dict[str, Dict] = {}
        async with httpx.AsyncClient(timeout=15) as client:
            for i in range(0, len(tokens), 50):  # FULL mode limit: 50 tokens/call
                batch = tokens[i:i + 50]

                await self._throttle(call_name=f"option_chain_quotes:{sym}:batch{i // 50 + 1}")
                if use_sdk:
                    # Prefer the SDK's own getMarketData() over a hand-rolled
                    # REST call — get_ltp() above already proves this
                    # SmartConnect instance's auth works via the SDK, but our
                    # own manually-built Authorization/X-PrivateKey headers
                    # got "Invalid Token" from the raw REST endpoint. Letting
                    # the SDK manage its own auth avoids that mismatch.
                    try:
                        # Keep the original SmartConnect object so recovery can detect
                        # whether another coroutine already recovered the session.
                        failed_obj = self._obj
                        body = await asyncio.to_thread(
                            self._obj.getMarketData,
                            mode="FULL", exchangeTokens={derivative_exchange: batch}
                        )
                    except Exception as e:
                        if not self._is_invalid_token(e):
                            raise AngelOneError(
                                f"SDK getMarketData failed: {e}"
                            )

                        logger.warning(
                            "Angel One Invalid Token on option-chain batch %s "
                            "for %s; recovering session",
                            i // 50 + 1,
                            sym,
                        )

                        if not await self._recover_invalid_token(
                            failed_obj=failed_obj
                        ):
                            raise AngelOneError(
                                "Option-chain quote fetch failed: "
                                "Invalid Token and session recovery failed"
                            )

                        try:
                            await self._throttle(
                                call_name=f"option_chain_quotes:{sym}:batch{i // 50 + 1}"
                            )
                            body = await asyncio.to_thread(
                                self._obj.getMarketData,
                                mode="FULL", exchangeTokens={derivative_exchange: batch}
                            )
                        except Exception as retry_error:
                            raise AngelOneError(
                                "Option-chain quote fetch failed after "
                                f"session recovery: {retry_error}"
                            )
                else:
                    # FIX (2026-08-20): REST fallback-ல் explicit WARNING —
                    # இது "Invalid Token" error-உடன் fail ஆகும்.
                    # getMarketData SDK method இல்லன்னா இங்கே வரும்.
                    logger.warning(
                        f"Angel One REST quote fallback for batch {i//50 + 1} "
                        f"({len(batch)} tokens) — likely to fail with 'Invalid Token'. "
                        f"Check logs below for exact error."
                    )
                    resp = await client.post(
                        self.QUOTE_URL,
                        headers=self._quote_headers(),
                        json={"mode": "FULL", "exchangeTokens": {derivative_exchange: batch}},
                    )
                    body = resp.json() if resp.content else {}
                    # REST response log — exact error visible in logs
                    if isinstance(body, dict) and body.get("status") is False:
                        logger.error(
                            f"Angel One REST quote error: "
                            f"status={body.get('status')}, "
                            f"message={body.get('message')!r}, "
                            f"errorcode={body.get('errorcode')!r}"
                        )

                if not isinstance(body, dict) or not body:
                    raise AngelOneError(
                        "Quote fetch error: "
                        f"body-type={type(body).__name__}, body={str(body)[:200]}"
                    )

                # Angel One can return AG8001 as a normal JSON response.
                # Recover the stale session and retry this batch exactly once.
                if body.get("status") is False:
                    error_text = (
                        body.get("errorcode")
                        or body.get("message")
                        or body.get("error")
                        or ""
                    )

                    if self._is_invalid_token(error_text):
                        logger.warning(
                            "Angel One Invalid Token response on option-chain batch %s "
                            "for %s; recovering session",
                            i // 50 + 1,
                            sym,
                        )

                        failed_obj = self._obj
                        if not await self._recover_invalid_token(
                            failed_obj=failed_obj
                        ):
                            raise AngelOneError(
                                "Option-chain quote fetch failed: "
                                "Invalid Token and session recovery failed"
                            )

                        try:
                            await self._throttle(
                                call_name=f"option_chain_quotes:{sym}:batch{i // 50 + 1}"
                            )
                            body = await asyncio.to_thread(
                                self._obj.getMarketData,
                                mode="FULL", exchangeTokens={derivative_exchange: batch}
                            )
                        except Exception as retry_error:
                            raise AngelOneError(
                                "Option-chain quote fetch failed after "
                                f"session recovery: {retry_error}"
                            )

                        if not isinstance(body, dict) or not body:
                            raise AngelOneError(
                                "Option-chain quote fetch returned invalid response "
                                "after session recovery"
                            )

                        if body.get("status") is False:
                            retry_text = (
                                body.get("errorcode")
                                or body.get("message")
                                or "Unknown"
                            )
                            raise AngelOneError(
                                "Option-chain quote fetch failed after session "
                                f"recovery: {retry_text}"
                            )
                    else:
                        msg = body.get("message", "Unknown")
                        raise AngelOneError(f"Quote fetch error: {msg}")
                data_block = body.get("data")
                if isinstance(data_block, dict):
                    fetched = data_block.get("fetched", [])
                elif isinstance(data_block, list):
                    fetched = data_block
                else:
                    raise AngelOneError(
                        f"Unexpected quote 'data' shape: {type(data_block).__name__} "
                        f"(value: {str(data_block)[:200]!r}, "
                        f"message: {body.get('message')!r}, "
                        f"errorcode: {body.get('errorcode')!r}, "
                        f"status: {body.get('status')!r}, "
                        f"via: {'sdk' if use_sdk else 'rest'})"
                    )

                for item in fetched:
                    if not isinstance(item, dict):
                        logger.warning(f"Skipping non-dict quote item: {type(item).__name__} = {str(item)[:100]}")
                        continue
                    # FIX (2026-08-26): getMarketData returns "symbolToken" (camelCase)
                    # but we stored instrument-master "token" as strings. Normalize
                    # BOTH sides to str so "12345" == "12345" always matches.
                    token = str(item.get("symbolToken") or item.get("symboltoken") or "")
                    if token:
                        quotes[token] = item
                        if futures_token and token == futures_token:
                            try:
                                ltp = safe_float(item.get("ltp", 0))
                                if ltp > 0:
                                    cache = getattr(self, "_bundled_futures_quote_cache", None)
                                    if cache is None:
                                        cache = {}
                                        self._bundled_futures_quote_cache = cache
                                    cache[sym] = {"token": token, "ltp": ltp, "expiry": futures_info.get("expiry", "") if futures_info else "", "tradingsymbol": futures_info.get("tradingsymbol", "") if futures_info else "", "ts": time.monotonic()}
                            except Exception:
                                pass

        logger.info(f"Angel One option chain quotes fetched: {len(quotes)} tokens matched out of {len(tokens)}")

        strike_map: Dict[float, Dict] = {}
        for r in selected_rows:
            token = str(r.get("token") or "")   # FIX: str() to match quotes dict keys
            q = quotes.get(token)
            if not q:
                continue
            strike = _strike(r)
            tsym = r.get("symbol", "")
            opt_type = "CE" if tsym.endswith("CE") else "PE" if tsym.endswith("PE") else None
            if not opt_type:
                continue
            # CHAIN_IV_DERIVATION_FROM_LTP_20260925
            chain_iv = 0.0
            try:
                dte = days_to_expiry(chosen_expiry)
                option_ltp = safe_float(q.get("ltp", 0))
                if (
                    dte is not None
                    and dte > 0
                    and spot > 0
                    and strike > 0
                    and option_ltp > 0
                ):
                    derived_iv = implied_volatility_from_price(
                        spot=spot,
                        strike=strike,
                        days_to_expiry=dte,
                        option_price=option_ltp,
                        option_type=opt_type,
                    )
                    if derived_iv is not None and derived_iv > 0:
                        chain_iv = derived_iv
            except Exception:
                chain_iv = 0.0

            depth = q.get("depth") or {}
            buy_depth = depth.get("buy") or []
            sell_depth = depth.get("sell") or []

            best_bid = None
            best_ask = None

            if isinstance(buy_depth, list) and buy_depth:
                first_buy = buy_depth[0]
                if isinstance(first_buy, dict):
                    best_bid = first_buy.get("price")

            if isinstance(sell_depth, list) and sell_depth:
                first_sell = sell_depth[0]
                if isinstance(first_sell, dict):
                    best_ask = first_sell.get("price")

            leg = {
                "strikePrice":          strike,
                "expiryDate":           chosen_expiry,
                "token":                token,
                "tradingsymbol":        tsym,
                "optionType":           opt_type,
                "bidprice":            safe_float(best_bid or 0),
                "askPrice":            safe_float(best_ask or 0),
                "bid":                 safe_float(best_bid or 0),
                "ask":                 safe_float(best_ask or 0),
                "totalBuyQuantity":    safe_float(q.get("totBuyQuan", 0)),
                "totalSellQuantity":   safe_float(q.get("totSellQuan", 0)),
                "exchangeFeedTime":    q.get("exchFeedTime"),
                "exchangeTradeTime":   q.get("exchTradeTime"),
                "openInterest":         safe_float(q.get("opnInterest", q.get("openInterest", 0))),
                "changeinOpenInterest": safe_float(q.get("opnInterestChange", 0)),
                "lastPrice":            safe_float(q.get("ltp", 0)),
                "change":               safe_float(q.get("netChange", 0)),
                "impliedVolatility":    chain_iv,
                "totalTradedVolume":    safe_float(q.get("tradeVolume", 0)),
            }
            row = strike_map.setdefault(strike, {"strikePrice": strike, "expiryDate": chosen_expiry})
            row[opt_type] = leg

        chain_rows = [strike_map[s] for s in sorted(strike_map.keys())]
        if not chain_rows:
            raise AngelOneError("Quote batch returned no matchable rows")

        # LOT_SIZE_SOURCE_OF_TRUTH_20260925
        # Authoritative contract size from the selected Angel
        # instrument-master rows for this exact symbol/expiry.
        lot_sizes = set()
        for _r in selected_rows:
            try:
                _lot = int(float(_r.get("lotsize") or 0))
            except (TypeError, ValueError):
                _lot = 0
            if _lot > 0:
                lot_sizes.add(_lot)

        lot_size = next(iter(lot_sizes)) if len(lot_sizes) == 1 else 0

        logger.info(
            f"Angel One option chain built: {len(chain_rows)} strikes "
            f"for {sym} {chosen_expiry}, lot_size={lot_size}"
        )
        return {
            "symbol":           sym,
            "expiry":           chosen_expiry,
            "all_expiries":     expiries,
            "underlying_price": spot,
            "lot_size":         lot_size,
            "data":             chain_rows,
            "data_source":      "angel_one_composed",
        }

    async def get_candle_data(
        self,
        symbol: str,
        interval: str = "ONE_DAY",
        from_date: str = "",
        to_date: str = "",
        fail_fast_rate_limit: bool = False,
    ) -> List[Dict]:
        """
        Historical OHLCV candle data for the index itself.
        interval: ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE,
                  THIRTY_MINUTE, ONE_HOUR, ONE_DAY
        from_date / to_date: "YYYY-MM-DD HH:MM"

        NOTE: NSE indices (NIFTY 50, BANK NIFTY, FINNIFTY) aren't traded
        directly, so `volume` in the returned candles is usually 0. For a
        real volume series, use get_futures_candle_data() instead — see its
        docstring.
        """
        await self.ensure_session()
        info = self.SYMBOL_TOKENS.get(symbol.upper())
        if not info:
            raise AngelOneError(f"Unknown symbol: {symbol}")
        return await self._fetch_candles(
            info["exchange"],
            info["token"],
            interval,
            from_date,
            to_date,
            label=symbol.upper(),
            fail_fast_rate_limit=fail_fast_rate_limit,
        )

    async def get_futures_candle_data(
        self,
        symbol: str,
        interval: str = "ONE_DAY",
        from_date: str = "",
        to_date: str = "",
    ) -> List[Dict]:
        """
        Historical OHLCV candles for `symbol`'s nearest-expiry NFO future.

        Used as a volume proxy: the index itself has no traded volume, but
        its front-month future does, and futures volume tracks the same
        underlying move closely enough to drive VWAP / volume-spike
        indicators. Resolves the token from the instrument master (see
        _resolve_futures_token) rather than hardcoding it, since the
        front-month contract changes every expiry.
        """
        info = await self._resolve_futures_token(symbol)
        if not info:
            raise AngelOneError(f"No futures instrument found for {symbol}")
        return await self._fetch_candles(
            info["exchange"], info["token"], interval, from_date, to_date,
            label=f"{symbol.upper()} FUT"
        )

    async def _resolve_futures_token(self, symbol: str) -> Optional[Dict]:
        """
        Nearest-expiry NFO index-future token for `symbol`, resolved from
        the same instrument master used for options (see _ensure_instruments
        — one 24h-cached download covers both option and future lookups).
        """
        await self.ensure_session()
        await self._ensure_instruments()
        if not isinstance(self._instruments, list) or not self._instruments:
            return None

        sym = self.NFO_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        derivative_exchange = self.DERIVATIVE_EXCHANGE_MAP.get(
            symbol.upper(), "NFO"
        )
        rows = [
            r for r in self._instruments
            if isinstance(r, dict)
            and r.get("name") == sym
            and r.get("instrumenttype") == "FUTIDX"
            and r.get("exch_seg") == derivative_exchange
            and r.get("expiry")
        ]
        if not rows:
            return None

        # Nearest expiry = front-month contract (most liquid, closest volume
        # profile to what the index itself would show if it traded).
        def _expiry_key(r):
            for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(r["expiry"], fmt)
                except ValueError:
                    continue
            return datetime.max

        rows.sort(key=_expiry_key)
        nearest = rows[0]
        token = nearest.get("token")
        if not token:
            return None
        return {
            "token": token, "exchange": self.DERIVATIVE_EXCHANGE_MAP.get(symbol.upper(), "NFO"), "expiry": nearest.get("expiry"),
            "tradingsymbol": nearest.get("symbol", ""),
        }

    async def get_futures_ltp(self, symbol: str) -> Optional[Dict]:
        """
        Front-month NFO futures LTP for `symbol`.

        Uses Angel One getMarketData() first because the instrument-master
        NFO token belongs to the batch-quote namespace used by this endpoint.
        If Angel One returns AG8001 / Invalid Token, recover the session once
        and retry the same request once before falling back.

        Returns None when the quote cannot be obtained. Never fabricates
        a futures price.
        """
        if not self.is_configured:
            return None

        info = await self._resolve_futures_token(symbol)
        if not info or not info.get("token"):
            return None

        token = str(info["token"])
        sym = symbol.upper()
        derivative_exchange = self.DERIVATIVE_EXCHANGE_MAP.get(
            symbol.upper(), "NFO"
        )
        bundled_cache = getattr(self, "_bundled_futures_quote_cache", {})
        bundled = bundled_cache.get(sym)
        if isinstance(bundled, dict) and str(bundled.get("token", "")) == token:
            age = time.monotonic() - float(bundled.get("ts", 0) or 0)
            ltp = safe_float(bundled.get("ltp", 0))
            if ltp > 0 and age <= 15.0:
                logger.info("Angel One futures LTP reused from bundled option-chain quote: %s age=%.2fs", sym, age)
                return {"ltp": ltp, "expiry": bundled.get("expiry", info.get("expiry", "")), "tradingsymbol": bundled.get("tradingsymbol", info.get("tradingsymbol", ""))}
        use_sdk = hasattr(self._obj, "getMarketData")

        # ------------------------------------------------------------
        # Primary path: SDK getMarketData()
        # ------------------------------------------------------------
        if use_sdk:
            failed_obj = self._obj

            try:
                await self._throttle(call_name=f"futures_ltp:{sym}")

                body = await asyncio.to_thread(
                    self._obj.getMarketData,
                    mode="LTP",
                    exchangeTokens={derivative_exchange: [token]},
                )

            except Exception as e:
                if self._is_invalid_token(e):
                    logger.warning(
                        "Angel One Invalid Token on futures LTP for %s; "
                        "recovering session",
                        sym,
                    )

                    recovered = await self._recover_invalid_token(
                        failed_obj=failed_obj
                    )

                    if recovered:
                        try:
                            await self._throttle(
                                call_name=f"futures_ltp:{sym}:retry"
                            )
                            body = await asyncio.to_thread(
                                self._obj.getMarketData,
                                mode="LTP",
                                exchangeTokens={derivative_exchange: [token]},
                            )
                        except Exception as retry_error:
                            if self._is_invalid_token(retry_error):
                                logger.warning(
                                    "Futures LTP retry still has Invalid Token "
                                    "for %s; falling back",
                                    sym,
                                )
                            else:
                                logger.debug(
                                    "Futures LTP retry failed for %s: %s",
                                    sym,
                                    retry_error,
                                )
                            body = None
                    else:
                        body = None
                else:
                    logger.debug(
                        "getMarketData futures LTP failed for %s: %s",
                        sym,
                        e,
                    )
                    body = None

            # --------------------------------------------------------
            # Normal successful response
            # --------------------------------------------------------
            if isinstance(body, dict) and body.get("status") is not False:
                data_block = body.get("data", {})
                fetched = (
                    data_block.get("fetched", [])
                    if isinstance(data_block, dict)
                    else data_block
                    if isinstance(data_block, list)
                    else []
                )

                for item in fetched:
                    if isinstance(item, dict):
                        ltp = safe_float(item.get("ltp", 0))
                        if ltp > 0:
                            return {
                                "ltp": ltp,
                                "expiry": info.get("expiry", ""),
                                "tradingsymbol": info.get(
                                    "tradingsymbol", ""
                                ),
                            }

            # --------------------------------------------------------
            # Angel One may return AG8001 as JSON status=False instead
            # of raising an exception.
            # --------------------------------------------------------
            if isinstance(body, dict) and body.get("status") is False:
                error_text = (
                    body.get("errorcode")
                    or body.get("message")
                    or body.get("error")
                    or ""
                )

                if self._is_invalid_token(error_text):
                    logger.warning(
                        "Angel One Invalid Token response on futures LTP "
                        "for %s; recovering session",
                        sym,
                    )

                    failed_obj = self._obj

                    if await self._recover_invalid_token(
                        failed_obj=failed_obj
                    ):
                        try:
                            await self._throttle(
                                call_name=f"futures_ltp:{sym}:retry"
                            )

                            retry_body = await asyncio.to_thread(
                                self._obj.getMarketData,
                                mode="LTP",
                                exchangeTokens={derivative_exchange: [token]},
                            )

                            if (
                                isinstance(retry_body, dict)
                                and retry_body.get("status") is not False
                            ):
                                data_block = retry_body.get("data", {})
                                fetched = (
                                    data_block.get("fetched", [])
                                    if isinstance(data_block, dict)
                                    else data_block
                                    if isinstance(data_block, list)
                                    else []
                                )

                                for item in fetched:
                                    if isinstance(item, dict):
                                        ltp = safe_float(
                                            item.get("ltp", 0)
                                        )
                                        if ltp > 0:
                                            return {
                                                "ltp": ltp,
                                                "expiry": info.get(
                                                    "expiry", ""
                                                ),
                                                "tradingsymbol": info.get(
                                                    "tradingsymbol", ""
                                                ),
                                            }

                        except Exception as retry_error:
                            logger.debug(
                                "Futures LTP retry failed for %s: %s",
                                sym,
                                retry_error,
                            )

                else:
                    logger.debug(
                        "getMarketData futures LTP returned error for %s: %s",
                        sym,
                        error_text,
                    )

        # ------------------------------------------------------------
        # Fallback: ltpData()
        # ------------------------------------------------------------
        if not info.get("tradingsymbol"):
            return None

        failed_obj = self._obj

        try:
            await self._throttle(
                call_name=f"futures_ltp_fallback:{sym}"
            )

            data = await asyncio.to_thread(
                self._obj.ltpData,
                info["exchange"],
                info["tradingsymbol"],
                token,
            )

        except Exception as e:
            if self._is_invalid_token(e):
                logger.warning(
                    "Angel One Invalid Token on futures ltpData fallback "
                    "for %s; recovering session",
                    sym,
                )

                if await self._recover_invalid_token(
                    failed_obj=failed_obj
                ):
                    try:
                        await self._throttle(
                            call_name=f"futures_ltp_fallback:{sym}:retry"
                        )

                        data = await asyncio.to_thread(
                            self._obj.ltpData,
                            info["exchange"],
                            info["tradingsymbol"],
                            token,
                        )

                    except Exception as retry_error:
                        logger.debug(
                            "Futures ltpData retry failed for %s: %s",
                            sym,
                            retry_error,
                        )
                        return None
                else:
                    return None
            else:
                logger.debug(
                    "Futures ltpData failed for %s: %s",
                    sym,
                    e,
                )
                return None

        if not isinstance(data, dict):
            return None

        if data.get("status") is False:
            error_text = (
                data.get("errorcode")
                or data.get("message")
                or data.get("error")
                or ""
            )

            if self._is_invalid_token(error_text):
                logger.warning(
                    "Angel One Invalid Token response on futures "
                    "ltpData fallback for %s; recovering session",
                    sym,
                )

                failed_obj = self._obj

                if await self._recover_invalid_token(
                    failed_obj=failed_obj
                ):
                    try:
                        await self._throttle(
                            call_name=f"futures_ltp_fallback:{sym}:retry"
                        )

                        retry_data = await asyncio.to_thread(
                            self._obj.ltpData,
                            info["exchange"],
                            info["tradingsymbol"],
                            token,
                        )

                        if (
                            isinstance(retry_data, dict)
                            and retry_data.get("status") is not False
                        ):
                            data = retry_data
                        else:
                            return None

                    except Exception as retry_error:
                        logger.debug(
                            "Futures ltpData response retry failed "
                            "for %s: %s",
                            sym,
                            retry_error,
                        )
                        return None
                else:
                    return None
            else:
                logger.debug(
                    "Futures LTP error for %s: %s",
                    sym,
                    error_text,
                )
                return None

        ltp_data = data.get("data", {})
        if not isinstance(ltp_data, dict):
            return None

        ltp = safe_float(ltp_data.get("ltp", 0))
        if ltp <= 0:
            return None

        return {
            "ltp": ltp,
            "expiry": info.get("expiry", ""),
            "tradingsymbol": info["tradingsymbol"],
        }

    async def _fetch_candles(
        self,
        exchange: str,
        token: str,
        interval: str,
        from_date: str,
        to_date: str,
        label: str = "",
        fail_fast_rate_limit: bool = False,
    ) -> List[Dict]:
        """
        Shared low-level candle fetch used by get_candle_data() (index) and
        get_futures_candle_data() (futures proxy for volume). Both go
        through the same rate limiter and rate-limit retry so callers don't
        have to duplicate throttling logic.
        """
        # FIX 2026-09-11:
        # Use IST for Angel One candle fallback/default dates.
        # This prevents UTC/IST mismatch from creating an invalid
        # candle range where fromdate is later than todate.
        current_ist = now_ist()

        if not from_date:
            from_date = (
                current_ist - timedelta(days=60)
            ).strftime("%Y-%m-%d 09:15")

        if not to_date:
            to_date = current_ist.strftime("%Y-%m-%d %H:%M")

        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_date,
            "todate": to_date,
        }

        def _is_rate_limit(value) -> bool:
            text = str(value).lower()
            return any(marker in text for marker in (
                "ab1021",
                "too many requests",
                "rate limit",
                "access rate",
                "exceeding access rate",
                "access denied",
            ))

        async def _call_candle_api():
            # Serialize the complete historical broker transaction.
            # _throttle() alone only reserves a start slot; it does not
            # prevent another historical request from starting while this
            # SDK call is still in-flight.
            async with self._historical_api_lock:
                await self._throttle(call_name=f"candle:{label}:{interval}")
                # FIX (event-loop block): getCandleData() is a blocking
                # `requests` call — offload it to a thread.
                return await asyncio.to_thread(
                    self._obj.getCandleData,
                    params,
                )

        # First attempt.
        # AG8001 Invalid Token must be recovered separately from the
        # normal AB1021/rate-limit retry path.
        failed_obj = self._obj

        try:
            resp = await _call_candle_api()

        except Exception as e:
            if self._is_invalid_token(e):
                logger.warning(
                    "Angel One Invalid Token on candle:%s:%s; "
                    "attempting session recovery",
                    label or token,
                    interval,
                )

                if not await self._recover_invalid_token(
                    failed_obj=failed_obj
                ):
                    raise AngelOneError(
                        f"Candle data fetch failed for {label or token}: "
                        "Invalid Token and session recovery failed"
                    )

                # Retry exactly once after successful session recovery.
                try:
                    resp = await _call_candle_api()
                except Exception as retry_error:
                    raise AngelOneError(
                        f"Candle data fetch failed after session recovery "
                        f"for {label or token}: {retry_error}"
                    )

            elif not _is_rate_limit(e):
                raise AngelOneError(
                    f"Candle data fetch failed: {e}"
                )

            else:
                priority = get_angel_call_priority()

                # Background history collection must never poison the
                # foreground broker path with a global cooldown.
                if priority == ANGEL_PRIORITY_BACKGROUND:
                    logger.warning(
                        "Angel One background rate limit hit for %s — "
                        "skipping retry/cooldown",
                        label or token,
                    )
                    raise AngelOneError(
                        f"Background Angel One rate limit for "
                        f"{label or token}"
                    )

                if fail_fast_rate_limit:
                    # Even on fail-fast fallback, pause OTHER foreground
                    # Angel calls sharing this session. Otherwise concurrent
                    # calls can immediately walk into the same broker limit.
                    self._rate_limit_cooldown_until = max(
                        self._rate_limit_cooldown_until,
                        time.time() + 8.0,
                    )
                    logger.warning(
                        "Angel One rate limit hit for %s — "
                        "shared 8s cooldown + fail-fast fallback",
                        label or token,
                    )
                    raise AngelOneError(
                        f"Angel One rate limit for {label or token}"
                    )


                logger.warning(
                    "Angel One rate limit hit for %s — "
                    "retrying once after backoff",
                    label or token,
                )
                self._rate_limit_cooldown_until = time.time() + 8.0
                await asyncio.sleep(
                    8.0 + random.uniform(0.0, 1.0)
                )

                try:
                    resp = await _call_candle_api()
                except Exception as e2:
                    raise AngelOneError(
                        f"Candle data fetch failed after retry: {e2}"
                    )

        # SmartAPI can report either AG8001 or a rate-limit failure
        # as a normal response instead of raising an exception.
        if not resp:
            raise AngelOneError("Candle data error: No response")

        if resp.get("status") is False:
            message = str(resp.get("message", "Unknown"))
            errorcode = str(resp.get("errorcode", ""))
            error_text = f"{errorcode} {message}"

            # AG8001: recover the stale broker session first.
            if self._is_invalid_token(error_text):
                logger.warning(
                    "Angel One Invalid Token response on candle:%s:%s; "
                    "attempting session recovery",
                    label or token,
                    interval,
                )

                if not await self._recover_invalid_token(
                    failed_obj=failed_obj
                ):
                    raise AngelOneError(
                        f"Candle data error for {label or token}: "
                        "Invalid Token and session recovery failed"
                    )

                # Retry exactly once after session recovery.
                try:
                    resp = await _call_candle_api()
                except Exception as retry_error:
                    raise AngelOneError(
                        f"Candle data fetch failed after session recovery "
                        f"for {label or token}: {retry_error}"
                    )

                if not resp:
                    raise AngelOneError(
                        "Candle data error after session recovery: "
                        "No response"
                    )

                if resp.get("status") is False:
                    retry_errorcode = str(
                        resp.get("errorcode", "")
                    )
                    retry_message = str(
                        resp.get("message", "Unknown")
                    )
                    raise AngelOneError(
                        "Candle data error after session recovery: "
                        f"{retry_errorcode} {retry_message}".strip()
                    )

            # AB1021 / rate-limit response.
            elif _is_rate_limit(error_text):
                priority = get_angel_call_priority()

                if priority == ANGEL_PRIORITY_BACKGROUND:
                    logger.warning(
                        "Angel One background rate limit response for %s — "
                        "skipping retry/cooldown",
                        label or token,
                    )
                    raise AngelOneError(
                        f"Background Angel One rate limit for "
                        f"{label or token}: {message}"
                    )

                if fail_fast_rate_limit:
                    # SmartAPI may return rate-limit as a normal JSON
                    # response. Keep the shared cooldown consistent
                    # with the exception-based rate-limit path.
                    self._rate_limit_cooldown_until = max(
                        self._rate_limit_cooldown_until,
                        time.time() + 8.0,
                    )
                    logger.warning(
                        "Angel One rate limit response for %s — "
                        "shared 8s cooldown + fail-fast fallback",
                        label or token,
                    )
                    raise AngelOneError(
                        f"Angel One rate limit for {label or token}"
                    )


                logger.warning(
                    "Angel One rate limit response for %s — "
                    "retrying once after backoff",
                    label or token,
                )
                self._rate_limit_cooldown_until = time.time() + 8.0
                await asyncio.sleep(
                    8.0 + random.uniform(0.0, 1.0)
                )

                try:
                    resp = await _call_candle_api()
                except Exception as e2:
                    raise AngelOneError(
                        f"Candle data fetch failed after retry: {e2}"
                    )

                if not resp:
                    raise AngelOneError(
                        "Candle data error after retry: No response"
                    )

                if resp.get("status") is False:
                    retry_message = str(
                        resp.get("message", "Unknown")
                    )
                    raise AngelOneError(
                        f"Candle data error after retry: "
                        f"{retry_message}"
                    )

            else:
                raise AngelOneError(
                    f"Candle data error: {message}"
                )

        rows = resp.get("data", [])
        # Format: [timestamp, open, high, low, close, volume]
        return [
            {
                "timestamp": row[0],
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": int(row[5]) if len(row) > 5 else 0,
            }
            for row in rows if len(row) >= 5
        ]


    # ── Positions & Orders ──────────────────────────────────────────────

    async def get_positions(self) -> List[Dict]:
        """
        SmartAPI-லிருந்து live open positions fetch பண்ணும்.
        """
        import time
        _t0 = time.perf_counter()

        await self.ensure_session()
        _t_session = time.perf_counter()

        await self._throttle(priority=ANGEL_PRIORITY_HIGH, call_name="positions")
        _t_throttle = time.perf_counter()

        try:
            # FIX (event-loop block): offload blocking SDK call.
            resp = await asyncio.to_thread(self._obj.position)
        except Exception as e:
            raise AngelOneError(f"Positions fetch failed: {e}")

        _t_position = time.perf_counter()

        logger.info(
            "get_positions timing: session=%.3fs throttle=%.3fs angel_position=%.3fs total=%.3fs",
            _t_session - _t0,
            _t_throttle - _t_session,
            _t_position - _t_throttle,
            _t_position - _t0,
        )

        if not resp or resp.get("status") is False:
            raise AngelOneError(
                f"Positions error: {resp.get('message', 'Unknown') if resp else 'No response'}"
            )

        positions = []
        for r in resp.get("data") or []:
            netqty = int(safe_float(r.get("netqty", 0)))
            if netqty == 0:
                continue

            positions.append({
                "tradingsymbol":    r.get("tradingsymbol", "--"),
                "symboltoken":      r.get("symboltoken", ""),
                "exchange":         r.get("exchange", "NFO"),
                "producttype":      r.get("producttype", "INTRADAY"),
                "netqty":           abs(netqty),
                "averageprice":     safe_float(r.get("avgnetprice", 0)) or safe_float(r.get("netprice", 0)),
                "lasttradedprice":  safe_float(r.get("ltp", 0)),
                "buysell":          "BUY" if netqty > 0 else "SELL",
            })

        return positions

    # NOTE: square_off_position() (which called self._obj.placeOrder() to
    # place a real MARKET order) has been removed. This app is analysis/
    # information only — it never places, modifies, or squares off orders
    # with the broker. get_positions() above is read-only (viewing existing
    # holdings' live P&L), which is not order execution.


# ─── Singleton ─────────────────────────────────────────────────────────────
angel_session = AngelOneSession()
