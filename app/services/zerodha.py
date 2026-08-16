"""
Zerodha Kite Connect Integration
==================================
Second `MarketDataProvider` (spec §5) alongside Angel One — gives
`DataFetcher` a real fallback when Angel One is unconfigured/down AND NSE's
direct scrape is blocked, instead of the app going fully dark for the
"LIVE DATA UNAVAILABLE" state (CODE_REVIEW.md #15).

Fallback order in DataFetcher stays: Angel One → Zerodha → NSE scrape.
(Angel One first because it was already the established primary provider
in this app; Zerodha second because unlike NSE it's a real broker API, not
scraping — more resilient to IP blocks. Reorder is a one-line change in
data_fetcher.py's `_try_*` call order if you'd rather have Zerodha first.)

⚠️ STATUS: structural only, not exercised against a live Zerodha account —
no `KITE_API_KEY`/`KITE_API_SECRET`/`KITE_ACCESS_TOKEN` were available while
writing this. Field names below follow Kite Connect's public API docs
(https://kite.trade/docs/connect/v3/). Before relying on this in production:
  1. `pip install kiteconnect`
  2. Get an access token (see `login_url()` / `generate_session()` below —
     Kite's flow needs a one-time browser login + redirect, unlike Angel
     One's fully-headless TOTP login; the token is valid until ~06:00 IST
     the next day and must be regenerated daily, typically via a small
     scheduled script that stores the new token wherever `KITE_ACCESS_TOKEN`
     is read from).
  3. Set KITE_API_KEY / KITE_API_SECRET / KITE_ACCESS_TOKEN and check
     `GET /api/settings/health` + logs on the first real run — field names
     (`last_price`, `net_change` etc.) may need small adjustments the same
     way `angel_one.py`'s docstrings already flag for that provider.

No order placement here either — same read-only/analysis-only boundary as
the rest of this app (see angel_one.py's note on square_off_position()
having been removed entirely).
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from app.config import settings
from app.utils.helpers import safe_float, safe_int

logger = logging.getLogger(__name__)


class ZerodhaError(Exception):
    pass


class ZerodhaAuthError(ZerodhaError):
    pass


# Kite Connect's documented rate limit is ~3 requests/second for most
# endpoints (10/sec for order-related ones, not used here). Same pattern as
# Angel One's _MIN_CALL_INTERVAL — one shared throttle avoids concurrent
# asyncio.gather() calls (dashboard fetching NIFTY/BANKNIFTY/FINNIFTY at
# once) tripping the limit.
_MIN_CALL_INTERVAL = 0.35  # ~3/sec


class KiteSession:
    """Zerodha Kite Connect session wrapper — mirrors AngelOneSession's
    public shape (is_configured / get_ltp / get_india_vix / get_option_chain
    / get_candle_data) so data_fetcher.py can treat both providers
    interchangeably."""

    def __init__(self):
        self._kite = None
        self._rate_lock = asyncio.Lock()
        self._last_call_ts: float = 0.0
        self._instruments: Optional[List[Dict]] = None
        self._instruments_ts: float = 0.0
        self._instruments_lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(
            getattr(settings, "kite_api_key", None)
            and getattr(settings, "kite_access_token", None)
        )

    async def _throttle(self) -> None:
        async with self._rate_lock:
            now = time.time()
            wait = _MIN_CALL_INTERVAL - (now - self._last_call_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_ts = time.time()

    def _client(self):
        """Lazily builds the KiteConnect client. Import is deferred so the
        app runs fine without `kiteconnect` installed as long as Zerodha
        isn't configured (same pattern as angel_one.py's SmartApi import)."""
        if self._kite is not None:
            return self._kite
        if not self.is_configured:
            raise ZerodhaAuthError(
                "Zerodha not configured. .env-ல் KITE_API_KEY, KITE_API_SECRET, "
                "KITE_ACCESS_TOKEN சேர்க்கவும் (see zerodha.py module docstring)."
            )
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            raise ZerodhaError("kiteconnect package இல்லை. `pip install kiteconnect` run செய்யவும்.")
        kite = KiteConnect(api_key=settings.kite_api_key)
        kite.set_access_token(settings.kite_access_token)
        self._kite = kite
        return kite

    # ── One-time daily login helpers ─────────────────────────────────────
    # Kite Connect has no headless TOTP-only login like Angel One — it's an
    # OAuth-style browser redirect. These two helpers exist so a small
    # external script (run once per trading day, e.g. via cron before market
    # open) can complete that flow and hand the resulting access token to
    # this app via KITE_ACCESS_TOKEN — this app itself never opens a browser.

    def login_url(self) -> str:
        """URL to redirect a human to for the one-time daily Kite login."""
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            raise ZerodhaError("kiteconnect package இல்லை. `pip install kiteconnect` run செய்யவும்.")
        if not settings.kite_api_key:
            raise ZerodhaAuthError("KITE_API_KEY not configured.")
        return KiteConnect(api_key=settings.kite_api_key).login_url()

    def generate_session(self, request_token: str) -> str:
        """Exchanges the `request_token` from Kite's login redirect for an
        access token. Returns the access token string — the caller (your
        daily login script) is responsible for storing it as
        KITE_ACCESS_TOKEN for this app to pick up. Not called anywhere in
        this app's own request path."""
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            raise ZerodhaError("kiteconnect package இல்லை. `pip install kiteconnect` run செய்யவும்.")
        if not (settings.kite_api_key and settings.kite_api_secret):
            raise ZerodhaAuthError("KITE_API_KEY / KITE_API_SECRET not configured.")
        kite = KiteConnect(api_key=settings.kite_api_key)
        data = kite.generate_session(request_token, api_secret=settings.kite_api_secret)
        return data["access_token"]

    # ── Market data ───────────────────────────────────────────────────────

    # Kite instrument tokens for the NSE indices this app tracks. These are
    # Zerodha's well-known, long-stable public index tokens; confirm against
    # `kite.instruments("NSE")` if any ever fail to quote.
    INDEX_TOKENS = {
        "NIFTY":     256265,   # NSE:NIFTY 50
        "BANKNIFTY": 260105,   # NSE:NIFTY BANK
        "FINNIFTY":  257801,   # NSE:NIFTY FIN SERVICE
    }
    INDEX_TRADINGSYMBOL = {
        "NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE",
    }
    INDIA_VIX_TOKEN = 264969  # NSE:INDIA VIX

    async def get_ltp(self, symbol: str) -> Dict:
        """Live spot quote via Kite's quote() endpoint (fuller than ltp() —
        includes ohlc block, which get_spot needs for high/low/open/prev_close)."""
        token = self.INDEX_TOKENS.get(symbol.upper())
        if not token:
            raise ZerodhaError(f"Unknown symbol: {symbol}")
        kite = self._client()
        await self._throttle()
        key = f"NSE:{self.INDEX_TRADINGSYMBOL.get(symbol.upper(), symbol.upper())}"
        try:
            data = await asyncio.to_thread(kite.quote, [key])
        except Exception as e:
            raise ZerodhaError(f"Quote fetch failed for {symbol}: {e}")
        row = (data or {}).get(key)
        if not row:
            raise ZerodhaError(f"No quote row returned for {symbol} ({key})")
        ohlc = row.get("ohlc", {})
        last_price = safe_float(row.get("last_price"))
        prev_close = safe_float(ohlc.get("close"))
        return {
            "symbol": symbol.upper(),
            "price": last_price,
            "open": safe_float(ohlc.get("open")),
            "high": safe_float(ohlc.get("high")),
            "low": safe_float(ohlc.get("low")),
            "close": prev_close,
            "change": last_price - prev_close,
            "change_percent": round((last_price - prev_close) / max(prev_close, 1) * 100, 2),
            "source": "zerodha",
            "timestamp": datetime.now().isoformat(),
        }

    async def get_india_vix(self) -> float:
        kite = self._client()
        await self._throttle()
        key = "NSE:INDIA VIX"
        try:
            data = await asyncio.to_thread(kite.quote, [key])
        except Exception as e:
            raise ZerodhaError(f"India VIX fetch failed: {e}")
        row = (data or {}).get(key)
        val = safe_float((row or {}).get("last_price", 0))
        if val <= 0:
            raise ZerodhaError("India VIX returned 0")
        return val

    async def get_candle_data(
        self, symbol: str, interval: str = "day",
        from_date: Optional[str] = None, to_date: Optional[str] = None,
    ) -> List[Dict]:
        """Historical/intraday candles for the index itself.
        `interval`: one of Kite's own strings — "minute", "5minute",
        "15minute", "60minute", "day" (NOT Angel's "FIVE_MINUTE" style —
        data_fetcher.py's Zerodha wrappers translate between the two so the
        rest of the app doesn't need to know either provider's exact
        vocabulary)."""
        token = self.INDEX_TOKENS.get(symbol.upper())
        if not token:
            raise ZerodhaError(f"Unknown symbol: {symbol}")
        kite = self._client()

        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not from_date:
            from_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")

        await self._throttle()
        try:
            rows = await asyncio.to_thread(
                kite.historical_data, token, from_date, to_date, interval
            )
        except Exception as e:
            raise ZerodhaError(f"Historical data fetch failed for {symbol}: {e}")

        return [
            {
                "timestamp": str(r.get("date", "")),
                "open": safe_float(r.get("open")),
                "high": safe_float(r.get("high")),
                "low": safe_float(r.get("low")),
                "close": safe_float(r.get("close")),
                "volume": safe_int(r.get("volume", 0)),
            }
            for r in (rows or [])
        ]

    # ── Instrument master (needed for option-chain strike → token lookup,
    # same reason Angel One needs one — Kite has no single "option chain"
    # endpoint either) ────────────────────────────────────────────────────

    async def _ensure_instruments(self) -> None:
        if self._instruments and (time.time() - self._instruments_ts) < 86400:
            return
        async with self._instruments_lock:
            if self._instruments and (time.time() - self._instruments_ts) < 86400:
                return
            kite = self._client()
            await self._throttle()
            try:
                rows = await asyncio.to_thread(kite.instruments, "NFO")
            except Exception as e:
                raise ZerodhaError(f"Instrument list fetch failed: {e}")
            if not isinstance(rows, list) or not rows:
                raise ZerodhaError(f"Unexpected instruments response shape: {type(rows).__name__}")
            self._instruments = rows
            self._instruments_ts = time.time()
            logger.info(f"Zerodha NFO instrument list loaded — {len(self._instruments)} rows")

    NFO_SYMBOL_MAP = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "FINNIFTY": "FINNIFTY"}

    async def get_option_chain(
        self, symbol: str, expiry: Optional[str] = None, strikes_each_side: int = 15
    ) -> Dict:
        """Same hand-built approach as angel_one.py's get_option_chain:
        filter the instrument master to this symbol's option rows, pick an
        expiry, take strikes near ATM, batch-quote them, and reassemble into
        the {"strikePrice", "CE": {...}, "PE": {...}} shape option_analyzer.py
        already expects from NSE."""
        await self._ensure_instruments()
        kite = self._client()

        sym = self.NFO_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        rows = [
            r for r in self._instruments
            if isinstance(r, dict) and r.get("name") == sym and r.get("instrument_type") in ("CE", "PE")
        ]
        if not rows:
            raise ZerodhaError(f"No option instruments found for {sym}")

        expiries = sorted({str(r["expiry"]) for r in rows if r.get("expiry")})
        if not expiries:
            raise ZerodhaError(f"No expiries found for {sym}")
        chosen_expiry = expiry or expiries[0]
        rows = [r for r in rows if str(r.get("expiry")) == chosen_expiry]
        if not rows:
            raise ZerodhaError(f"No option rows for {sym} expiry {chosen_expiry}")

        spot_info = await self.get_ltp(symbol)
        spot = spot_info["price"]

        strikes = sorted({safe_float(r.get("strike", 0)) for r in rows if safe_float(r.get("strike", 0)) > 0})
        if not strikes:
            raise ZerodhaError(f"No valid strikes parsed for {sym} {chosen_expiry}")
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        lo = max(0, atm_idx - strikes_each_side)
        hi = min(len(strikes), atm_idx + strikes_each_side + 1)
        selected_strikes = set(strikes[lo:hi])

        selected_rows = [r for r in rows if safe_float(r.get("strike", 0)) in selected_strikes]
        keys = [f"NFO:{r['tradingsymbol']}" for r in selected_rows if r.get("tradingsymbol")]
        if not keys:
            raise ZerodhaError(f"No tradingsymbols resolved for {sym} {chosen_expiry}")

        quotes: Dict[str, Dict] = {}
        for i in range(0, len(keys), 500):  # Kite quote() batch limit
            batch = keys[i:i + 500]
            await self._throttle()
            try:
                resp = await asyncio.to_thread(kite.quote, batch)
            except Exception as e:
                raise ZerodhaError(f"Option quote batch fetch failed: {e}")
            quotes.update(resp or {})

        by_strike: Dict[float, Dict] = {}
        for r in selected_rows:
            key = f"NFO:{r.get('tradingsymbol', '')}"
            q = quotes.get(key)
            if not q:
                continue
            strike = safe_float(r.get("strike", 0))
            opt_type = r.get("instrument_type")  # "CE" or "PE"
            depth = q.get("depth", {})
            best_bid = safe_float((depth.get("buy") or [{}])[0].get("price", 0)) if depth else 0.0
            best_ask = safe_float((depth.get("sell") or [{}])[0].get("price", 0)) if depth else 0.0
            leg = {
                "openInterest": safe_int(q.get("oi", 0)),
                # Kite's quote() endpoint does not return a prior-day OI
                # baseline (unlike NSE's changeinOpenInterest / Angel's
                # equivalent) — there is no reliable way to compute today's
                # OI change from this single call. 0 here means "not
                # available", not "no change" — option_analyzer.py's
                # compute_oi_change() should be read with that in mind for
                # zerodha-sourced chains specifically.
                "changeinOpenInterest": 0,
                "totalTradedVolume": safe_int(q.get("volume", 0)),
                "lastPrice": safe_float(q.get("last_price", 0)),
                "impliedVolatility": 0.0,  # Kite quote() doesn't return IV directly — see note below
                "bidprice": best_bid, "askPrice": best_ask,
            }
            by_strike.setdefault(strike, {"strikePrice": strike, "expiryDate": chosen_expiry})[opt_type] = leg

        rows_out = [v for v in by_strike.values() if "CE" in v or "PE" in v]
        if not rows_out:
            raise ZerodhaError(f"No usable option rows assembled for {sym} {chosen_expiry}")

        return {
            "symbol": sym, "expiry": chosen_expiry, "all_expiries": expiries,
            "underlying_price": spot, "data": rows_out,
        }
        # NOTE on impliedVolatility=0.0 and changeinOpenInterest=0 above:
        # Kite's quote() endpoint doesn't return either directly (unlike
        # NSE/Angel). option_analyzer.py and strategy_engine.py that read
        # these fields should treat 0 from a zerodha-sourced chain as "not
        # available" rather than a real reading. Computing IV properly needs
        # a Black-Scholes solve against `lastPrice`; OI-change needs a
        # second snapshot to diff against (this app's own
        # history_service.get_oi_change_since already does exactly that
        # independently of whichever provider supplied the raw chain — see
        # market_analyzer.py's oi_change_tracked, which isn't affected by
        # this gap at all). Both are reasonable follow-ups but out of scope
        # for this structural pass.


# ─── Singleton ─────────────────────────────────────────────────────────────
kite_session = KiteSession()
