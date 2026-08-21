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
import logging
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
_MIN_CALL_INTERVAL = 1.0  # seconds between any two Angel One SmartAPI calls


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
                data = self._obj.generateSession(
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
            self._feed_token = self._obj.getfeedToken()
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
                resp = self._obj.terminateSession(settings.angel_client_id)
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
            data = self._obj.ltpData(
                info["exchange"], symbol.upper(), info["token"]
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
            data = self._obj.ltpData("NSE", "India VIX", self.INDIA_VIX_TOKEN)
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

    async def _ensure_instruments(self) -> None:
        """
        Angel One has no single "get option chain" API call — unlike NSE,
        option data has to be fetched strike-by-strike via the Market Quote
        API, and each strike needs its numeric instrument `token`. Angel
        publishes those tokens in a daily public JSON file (~100k+ rows,
        no auth needed). We cache it for 24h since it only changes once a
        day (new listings/expiries roll in).
        """
        if self._instruments and (time.time() - self._instruments_ts) < 86400:
            return
        async with self._instruments_lock:
            if self._instruments and (time.time() - self._instruments_ts) < 86400:
                return
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(self.INSTRUMENT_MASTER_URL)
                resp.raise_for_status()
                loaded = resp.json()
            if not isinstance(loaded, list):
                raise AngelOneError(
                    f"Instrument master response was not a list — got {type(loaded).__name__}. "
                    f"URL/format may have changed: {self.INSTRUMENT_MASTER_URL}"
                )
            self._instruments = loaded
            self._instruments_ts = time.time()
            logger.info(f"Angel One instrument master loaded — {len(self._instruments)} rows")

    def _quote_headers(self) -> Dict:
        return {
            "Authorization":     f"Bearer {self._auth_token}",
            "Content-Type":      "application/json",
            "Accept":            "application/json",
            "X-UserType":        "USER",
            "X-SourceID":        "WEB",
            "X-ClientLocalIP":   "127.0.0.1",
            "X-ClientPublicIP":  "127.0.0.1",
            "X-MACAddress":      "00:00:00:00:00:00",
            "X-PrivateKey":      settings.angel_api_key,
        }

    async def get_option_chain(
        self, symbol: str, expiry: Optional[str] = None, strikes_each_side: int = 15
    ) -> Dict:
        """
        Builds an NSE-shaped option chain by hand, since SmartAPI has no
        single "option chain" endpoint:
          1. Load the instrument master, filter to this symbol's OPTIDX rows.
          2. Pick the nearest expiry if none was given.
          3. Take `strikes_each_side` strikes above/below ATM (keeps the
             quote batch small — the full chain across every strike would
             be hundreds of tokens for no real benefit; traders look near
             ATM anyway).
          4. Batch-fetch CE+PE quotes via Angel's Market Quote API (FULL
             mode, ≤50 tokens/call) and merge into
             {"strikePrice", "expiryDate", "CE": {...}, "PE": {...}} rows —
             the same shape option_analyzer.py expects from NSE.

        Calls Angel's REST quote endpoint directly with httpx rather than
        through the SmartApi SDK, since the SDK's method name/response
        shape for batched quotes isn't reliably documented across versions.
        NOT verified against a live account — field names below follow
        Angel's public API docs but may need adjusting against real
        responses (check logs after enabling). Any failure here raises and
        the caller (data_fetcher._try_angel_option_chain) falls back to
        NSE — this never crashes the app either way.
        """
        await self.ensure_session()
        await self._ensure_instruments()

        if not isinstance(self._instruments, list) or not self._instruments:
            raise AngelOneError(
                f"Instrument master has unexpected shape: {type(self._instruments).__name__}"
            )
        if not isinstance(self._instruments[0], dict):
            raise AngelOneError(
                f"Instrument master rows are not dicts — got {type(self._instruments[0]).__name__} "
                f"(sample: {str(self._instruments[0])[:150]})"
            )

        sym = self.NFO_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        rows = [
            r for r in self._instruments
            if isinstance(r, dict)
            and r.get("name") == sym
            and r.get("instrumenttype") == "OPTIDX"
            and r.get("exch_seg") == "NFO"
        ]
        if not rows:
            raise AngelOneError(f"No option instruments found for {sym} in instrument master")

        expiries = sorted(set(r["expiry"] for r in rows if r.get("expiry")))
        if not expiries:
            raise AngelOneError(f"No expiries found for {sym}")

        # Normalize expiry comparison — Angel One instrument master may use
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
        tokens = [r["token"] for r in selected_rows if r.get("token")]
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
                        body = self._obj.getMarketData(mode="FULL", exchangeTokens={"NFO": batch})
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
                    token = item.get("symbolToken") or item.get("symboltoken")
                    if token:
                        quotes[token] = item

        logger.info(f"Angel One option chain quotes fetched: {len(quotes)} tokens matched out of {len(tokens)}")

        strike_map: Dict[float, Dict] = {}
        for r in selected_rows:
            token = r.get("token")
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
        """
        if not self.is_configured:
            return None
        info = await self._resolve_futures_token(symbol)
        if not info or not info.get("tradingsymbol"):
            return None
        await self._throttle()
        try:
            data = self._obj.ltpData(info["exchange"], info["tradingsymbol"], info["token"])
        except Exception as e:
            logger.warning(f"Futures LTP fetch failed for {symbol}: {e}")
            return None
        if not data or data.get("status") is False:
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
            resp = self._obj.getCandleData(params)
        except Exception as e:
            # "Access denied because of exceeding access rate" can still slip
            # through even with the throttle above (e.g. another request beat
            # this one to the lock right at the boundary). One retry after a
            # slightly longer wait clears it in practice without adding much
            # latency to the caller.
            if "exceeding access rate" in str(e).lower() or "access denied" in str(e).lower():
                logger.warning(f"Angel One rate limit hit for {label or token}, retrying once after backoff")
                await asyncio.sleep(1.5)
                await self._throttle()
                try:
                    resp = self._obj.getCandleData(params)
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
            resp = self._obj.position()
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