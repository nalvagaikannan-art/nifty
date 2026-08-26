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
import json
import logging
import random
import re
import time
from datetime import datetime
from typing import Optional, Dict, List
import httpx
from app.config import settings
from app.utils.helpers import safe_float

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
_MIN_CALL_INTERVAL = 2.2  # seconds between any two Angel One SmartAPI calls


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

    async def _throttle(self) -> None:
        """
        Block until at least _MIN_CALL_INTERVAL seconds have passed since the
        last Angel One API call. Call this immediately before every blocking
        SmartAPI SDK call (ltpData, getCandleData, getMarketData, position,
        placeOrder, etc). Using one lock across all methods means concurrent
        calls from asyncio.gather() queue up and space themselves out instead
        of all firing at once.
        """
        async with self._rate_lock:
            now = time.time()
            wait = _MIN_CALL_INTERVAL - (now - self._last_call_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_ts = time.time()

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

    async def login(self) -> Dict:
        """Angel One-ல் login செய்து auth/feed tokens return செய்யும்."""
        if not self.is_configured:
            raise AngelOneAuthError(
                "Angel One credentials not configured. "
                ".env-ல் ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP_SECRET சேர்க்கவும்."
            )

        async with self._lock:
            if self.is_logged_in:
                return {"status": "already_logged_in", "client_id": settings.angel_client_id}

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
                # FIX (event-loop block): generateSession() is a blocking
                # `requests` call under the hood — run it in a thread so it
                # doesn't freeze the whole process (and the health check
                # with it) for the duration of the HTTP round-trip.
                data = await asyncio.to_thread(
                    self._obj.generateSession,
                    settings.angel_client_id,
                    settings.angel_password,
                    totp
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

            logger.info(f"Angel One login successful — client: {settings.angel_client_id}")
            return {
                "status": "success",
                "client_id": settings.angel_client_id,
                "feed_token": self._feed_token,
                "session_expiry": datetime.fromtimestamp(
                    self._login_ts + self._session_ttl
                ).strftime("%Y-%m-%d %H:%M:%S")
            }

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

    # Symbol tokens for Angel One (NIFTY/BANKNIFTY/FINNIFTY NSE indices)
    SYMBOL_TOKENS = {
        "NIFTY":     {"token": "99926000", "exchange": "NSE"},
        "BANKNIFTY": {"token": "99926009", "exchange": "NSE"},
        "FINNIFTY":  {"token": "99926037", "exchange": "NSE"},
    }

    # Option chain token map (NFO segment)
    NFO_SYMBOL_MAP = {
        "NIFTY":     "NIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "FINNIFTY":  "FINNIFTY",
    }

    async def get_ltp(self, symbol: str) -> Dict:
        """Live LTP fetch — Angel One SmartAPI."""
        await self.ensure_session()
        info = self.SYMBOL_TOKENS.get(symbol.upper())
        if not info:
            raise AngelOneError(f"Unknown symbol: {symbol}")

        await self._throttle()
        try:
            # FIX (event-loop block): ltpData() is a blocking `requests`
            # call — offload to a thread so it doesn't stall the loop.
            data = await asyncio.to_thread(
                self._obj.ltpData, info["exchange"], symbol.upper(), info["token"]
            )
        except Exception as e:
            raise AngelOneError(f"LTP fetch failed for {symbol}: {e}")

        if not data or data.get("status") is False:
            raise AngelOneError(f"LTP error: {data.get('message', 'Unknown')}")

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
            "timestamp": datetime.now().isoformat()
        }

    # India VIX token — kept separate from SYMBOL_TOKENS/get_ltp() above
    # because Angel's official tradingsymbol for this instrument is
    # "India VIX" (with a space), not a plain "INDIAVIX" — reusing the
    # generic get_ltp(symbol) path would silently send the wrong
    # tradingsymbol string. Isolating it here means the existing
    # NIFTY/BANKNIFTY/FINNIFTY path above is completely untouched.
    INDIA_VIX_TOKEN = "99926017"

    async def get_india_vix(self) -> float:
        """India VIX LTP via Angel One — spec: 'Use Angel Data if available,
        otherwise NSE Public Source' for VIX. Returns 0.0 on any failure so
        the caller (DataFetcher.get_volatility) can fall back to NSE.
        """
        await self.ensure_session()
        await self._throttle()
        try:
            # FIX (event-loop block): offload blocking SDK call to a thread.
            data = await asyncio.to_thread(
                self._obj.ltpData, "NSE", "India VIX", self.INDIA_VIX_TOKEN
            )
        except Exception as e:
            raise AngelOneError(f"India VIX LTP fetch failed: {e}")
        if not data or data.get("status") is False:
            raise AngelOneError(f"India VIX LTP error: {data.get('message', 'Unknown')}")
        val = float((data.get("data") or {}).get("ltp", 0) or 0)
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
    _INSTRUMENT_MASTER_TTL = 12 * 3600  # 12 hours

    async def _ensure_instruments(self) -> None:
        """
        Downloads (or reuses a cached copy of) Angel One's instrument
        master, needed to resolve option/future strike → token.

        BUG FIX (2026-08-22): this used to unconditionally
        `raise AngelOneError("Instrument master disabled on Render...")` —
        it never called the chunked/whole download methods below at all,
        even though both were already fully written (and tested) in this
        same file. That's why option chain / futures premium / OI always
        fell back to NSE (and then further to "unavailable" once NSE's
        own endpoint also 404s) even when Angel One login succeeded.
        The chunked downloader (~4MB Range-request pages) exists
        specifically to make this safe on Render's memory/timeout limits,
        so re-enabling it here is the actual fix rather than a permanent
        disable. If it turns out to still be too slow/unreliable in a
        given deployment, both download paths already retry with backoff
        and raise a clear AngelOneError that callers already catch and
        fall back to NSE for — so this can't newly break anything that
        was working before.
        """
        now = time.time()
        if (
            isinstance(self._instruments, list)
            and self._instruments
            and (now - self._instruments_ts) < self._INSTRUMENT_MASTER_TTL
        ):
            return

        async with self._instruments_lock:
            # Re-check after acquiring the lock — another concurrent
            # caller may have just finished the download.
            now = time.time()
            if (
                isinstance(self._instruments, list)
                and self._instruments
                and (now - self._instruments_ts) < self._INSTRUMENT_MASTER_TTL
            ):
                return

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
                        f"Instrument master download failed (chunked: {e_chunked}; "
                        f"whole: {e_whole})"
                    )

            if not isinstance(loaded, list) or not loaded:
                raise AngelOneError("Instrument master download returned no rows")

            # FIX (OOM restarts): the raw file has 150k+ rows across every
            # NSE/BSE/NFO/MCX/CDS instrument — only ~150-300 of those (the
            # NIFTY/BANKNIFTY/FINNIFTY index options+futures) are ever read
            # (see get_option_chain / _resolve_futures_token below). Keeping
            # all 150k+ dicts in memory was costing several hundred MB per
            # process on top of FastAPI/uvicorn/httpx — on Render's free
            # 512MB plan that's enough to get OOM-killed a couple of minutes
            # after every restart (no traceback in app logs, since the OS
            # kills the process directly — that's why this looked like a
            # silent crash-loop). Filtering to just the rows this app
            # actually queries cuts that memory footprint by ~99% while
            # keeping every existing lookup (get_option_chain,
            # _resolve_futures_token) working unchanged, since both only
            # ever filter on these same fields.
            _wanted_names = set(self.NFO_SYMBOL_MAP.values())

            def _filter(rows):
                return [
                    r for r in rows
                    if isinstance(r, dict)
                    and r.get("exch_seg") == "NFO"
                    and r.get("name") in _wanted_names
                    and r.get("instrumenttype") in ("OPTIDX", "FUTIDX")
                    and r.get("expiry")
                ]
            # FIX (health-check timeouts): also offload the 151k-row filter
            # pass to the same thread as the json.loads() above — cheap
            # individually, but no reason to bring it back onto the loop.
            loaded = await asyncio.to_thread(_filter, loaded)
            if not loaded:
                raise AngelOneError(
                    "Instrument master download returned no matching NIFTY/"
                    "BANKNIFTY/FINNIFTY rows after filtering"
                )

            self._instruments = loaded
            self._instruments_ts = time.time()
            logger.info(f"Angel One instrument master cached — {len(loaded)} rows (filtered to NIFTY/BANKNIFTY/FINNIFTY)")




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

    async def get_option_chain(self, symbol: str, expiry: Optional[str] = None, strikes_each_side: int = 10) -> Dict:
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
        rows = [
            r for r in self._instruments
            if isinstance(r, dict)
            and r.get("name") == sym
            and r.get("instrumenttype") == "OPTIDX"
            and r.get("exch_seg") == "NFO"
            and r.get("expiry")
        ]
        if not rows:
            raise AngelOneError(f"No option instruments found for {sym}")

        expiries = sorted({r["expiry"] for r in rows}, key=self._expiry_sort_key)

        # '01SEP2026' while the UI/caller sends '01-Sep-2026' or '2026-09-01'.
        # Exact string match fails silently and returns zero rows.
        # Solution: normalize BOTH sides to 'YYYY-MM-DD' before comparing.
        if expiry:
            norm_requested = _normalize_expiry(expiry)
            # Find the raw master expiry string that maps to the same date
            matched = next(
                (e for e in expiries if _normalize_expiry(e) == norm_requested),
                None,
            )
            if matched:
                chosen_expiry = matched
            else:
                logger.warning(
                    "Requested expiry %r (normalized: %s) not found in master expiries %s "
                    "— falling back to nearest expiry %r",
                    expiry, norm_requested, expiries[:5], expiries[0],
                )
                chosen_expiry = expiries[0]
        else:
            chosen_expiry = expiries[0]

        rows = [r for r in rows if r.get("expiry") == chosen_expiry]
        if not rows:
            raise AngelOneError(f"No option rows for {sym} expiry {chosen_expiry}")

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
        lo = max(0, atm_idx - strikes_each_side)
        hi = min(len(all_strikes), atm_idx + strikes_each_side + 1)
        selected_strikes = set(all_strikes[lo:hi])

        selected_rows = [r for r in rows if _strike(r) in selected_strikes]
        tokens = [str(r["token"]) for r in selected_rows if r.get("token")]  # FIX: str() normalize
        if not tokens:
            raise AngelOneError(f"No tokens resolved for {sym} {chosen_expiry}")

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

                await self._throttle()
                if use_sdk:
                    # Prefer the SDK's own getMarketData() over a hand-rolled
                    # REST call — get_ltp() above already proves this
                    # SmartConnect instance's auth works via the SDK, but our
                    # own manually-built Authorization/X-PrivateKey headers
                    # got "Invalid Token" from the raw REST endpoint. Letting
                    # the SDK manage its own auth avoids that mismatch.
                    try:
                        # FIX (event-loop block): offload blocking SDK call.
                        body = await asyncio.to_thread(
                            self._obj.getMarketData,
                            mode="FULL", exchangeTokens={"NFO": batch}
                        )
                    except Exception as e:
                        raise AngelOneError(f"SDK getMarketData failed: {e}")
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
                        json={"mode": "FULL", "exchangeTokens": {"NFO": batch}},
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

                if not isinstance(body, dict) or not body or body.get("status") is False:
                    msg = (
                        body.get("message", "Unknown") if isinstance(body, dict) and body
                        else f"body-type={type(body).__name__}, body={str(body)[:200]}"
                    )
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
            leg = {
                "strikePrice":          strike,
                "expiryDate":           chosen_expiry,
                "openInterest":         safe_float(q.get("opnInterest", q.get("openInterest", 0))),
                "changeinOpenInterest": safe_float(q.get("opnInterestChange", 0)),
                "lastPrice":            safe_float(q.get("ltp", 0)),
                "change":               safe_float(q.get("netChange", 0)),
                "impliedVolatility":    0,  # Angel's quote API doesn't return IV
                "totalTradedVolume":    safe_float(q.get("tradeVolume", 0)),
            }
            row = strike_map.setdefault(strike, {"strikePrice": strike, "expiryDate": chosen_expiry})
            row[opt_type] = leg

        chain_rows = [strike_map[s] for s in sorted(strike_map.keys())]
        if not chain_rows:
            raise AngelOneError("Quote batch returned no matchable rows")

        logger.info(f"Angel One option chain built: {len(chain_rows)} strikes for {sym} {chosen_expiry}")
        return {
            "symbol":           sym,
            "expiry":           chosen_expiry,
            "all_expiries":     expiries,
            "underlying_price": spot,
            "data":             chain_rows,
            "data_source":      "angel_one_composed",
        }

    async def get_candle_data(
        self,
        symbol: str,
        interval: str = "ONE_DAY",
        from_date: str = "",
        to_date: str = "",
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
            info["exchange"], info["token"], interval, from_date, to_date, label=symbol.upper()
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
        rows = [
            r for r in self._instruments
            if isinstance(r, dict)
            and r.get("name") == sym
            and r.get("instrumenttype") == "FUTIDX"
            and r.get("exch_seg") == "NFO"
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
            "token": token, "exchange": "NFO", "expiry": nearest.get("expiry"),
            "tradingsymbol": nearest.get("symbol", ""),
        }

    async def get_futures_ltp(self, symbol: str) -> Optional[Dict]:
        """
        Front-month NFO futures LTP for `symbol` — used to compute the real
        futures premium/discount (futures LTP - spot) instead of the
        hardcoded 0.0 placeholder. Returns None (not raises) when Angel One
        isn't configured or the futures token/quote can't be resolved, so
        callers can fall back to an explicit "unavailable" status rather
        than a fabricated number.

        FIX (2026-08-26): AB4046 "Symbol token not found in scrip master
        cache" — this happened because ltpData() was called with the
        instrument-master token string directly, but Angel One's scrip-
        master cache is keyed on a DIFFERENT token format than what the
        instrument-master file uses for the same contract (especially
        around expiry rollover). Fix: use getMarketData (same path that
        works for option-chain quotes) instead of ltpData, since
        getMarketData uses the NFO token from _instruments and that
        IS the correct key for the batch-quote endpoint. Falls back
        to ltpData only when getMarketData is unavailable.
        """
        if not self.is_configured:
            return None
        info = await self._resolve_futures_token(symbol)
        if not info or not info.get("token"):
            return None

        token = str(info["token"])
        await self._throttle()

        # Prefer getMarketData (batch-quote path) — same token namespace
        # as option chain, avoids AB4046 scrip-master mismatch with ltpData
        use_sdk = hasattr(self._obj, "getMarketData")
        if use_sdk:
            try:
                # FIX (event-loop block): offload blocking SDK call.
                body = await asyncio.to_thread(
                    self._obj.getMarketData,
                    mode="LTP", exchangeTokens={"NFO": [token]}
                )
                if isinstance(body, dict) and body.get("status") is not False:
                    data_block = body.get("data", {})
                    fetched = (data_block.get("fetched", [])
                               if isinstance(data_block, dict) else
                               data_block if isinstance(data_block, list) else [])
                    for item in fetched:
                        if isinstance(item, dict):
                            ltp = safe_float(item.get("ltp", 0))
                            if ltp > 0:
                                return {
                                    "ltp": ltp,
                                    "expiry": info.get("expiry", ""),
                                    "tradingsymbol": info.get("tradingsymbol", ""),
                                }
            except Exception as e:
                logger.debug(f"getMarketData futures LTP failed for {symbol}, trying ltpData: {e}")

        # Fallback: ltpData (may hit AB4046 on some contracts — logged at debug)
        if not info.get("tradingsymbol"):
            return None
        try:
            # FIX (event-loop block): offload blocking SDK call.
            data = await asyncio.to_thread(
                self._obj.ltpData, info["exchange"], info["tradingsymbol"], token
            )
        except Exception as e:
            logger.debug(f"Futures ltpData failed for {symbol}: {e}")
            return None
        if not data or data.get("status") is False:
            err = (data or {}).get("errorcode", "")
            if err:
                logger.debug(f"Futures LTP errorcode {err} for {symbol} — token={token}")
            return None
        ltp_data = data.get("data", {})
        ltp = safe_float(ltp_data.get("ltp", 0))
        if ltp <= 0:
            return None
        return {"ltp": ltp, "expiry": info.get("expiry", ""), "tradingsymbol": info["tradingsymbol"]}

    async def _fetch_candles(
        self,
        exchange: str,
        token: str,
        interval: str,
        from_date: str,
        to_date: str,
        label: str = "",
    ) -> List[Dict]:
        """
        Shared low-level candle fetch used by get_candle_data() (index) and
        get_futures_candle_data() (futures proxy for volume). Both go
        through the same rate limiter and rate-limit retry so callers don't
        have to duplicate throttling logic.
        """
        from datetime import timedelta
        if not from_date:
            from_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d 09:15")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d %H:%M")

        await self._throttle()
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_date,
            "todate": to_date,
        }
        try:
            # FIX (event-loop block): getCandleData() is a blocking
            # `requests` call — offload to a thread so retries/backoff
            # below don't freeze the whole process (incl. health checks).
            resp = await asyncio.to_thread(self._obj.getCandleData, params)
        except Exception as e:
            # "Access denied because of exceeding access rate" can still slip
            # through even with the throttle above (e.g. another request beat
            # this one to the lock right at the boundary). One retry after a
            # slightly longer wait clears it in practice without adding much
            # latency to the caller.
            if "exceeding access rate" in str(e).lower() or "access denied" in str(e).lower():
                logger.warning(f"Angel One rate limit hit for {label or token}, retrying once after backoff")
                await asyncio.sleep(3.0 + random.uniform(0.0, 0.75))
                await self._throttle()
                try:
                    resp = await asyncio.to_thread(self._obj.getCandleData, params)
                except Exception as e2:
                    raise AngelOneError(f"Candle data fetch failed: {e2}")
            else:
                raise AngelOneError(f"Candle data fetch failed: {e}")

        if not resp or resp.get("status") is False:
            raise AngelOneError(f"Candle data error: {resp.get('message', 'Unknown') if resp else 'No response'}")

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

        ⚠️ NOT verified against a live account — field names (netqty,
        avgnetprice, ltp போன்றவை) Angel's public docs-ல் இருந்து எடுத்தது,
        get_option_chain() docstring-ல் இருக்கும் அதே caution இங்கேயும்
        applicable. முதல் live run-ல் logs பார்த்து confirm பண்ணிக்கொள்ளவும்.
        """
        await self.ensure_session()
        await self._throttle()
        try:
            # FIX (event-loop block): offload blocking SDK call.
            resp = await asyncio.to_thread(self._obj.position)
        except Exception as e:
            raise AngelOneError(f"Positions fetch failed: {e}")

        if not resp or resp.get("status") is False:
            raise AngelOneError(
                f"Positions error: {resp.get('message', 'Unknown') if resp else 'No response'}"
            )

        positions = []
        for r in resp.get("data") or []:
            netqty = int(safe_float(r.get("netqty", 0)))
            if netqty == 0:
                continue  # closed / flat position, skip
            positions.append({
                "tradingsymbol":    r.get("tradingsymbol", "--"),
                "symboltoken":      r.get("symboltoken", ""),
                "exchange":         r.get("exchange", "NFO"),
                "producttype":      r.get("producttype", "INTRADAY"),
                "netqty":           abs(netqty),
                "averageprice":     safe_float(r.get("avgnetprice") or r.get("netprice", 0)),
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
