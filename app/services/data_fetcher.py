"""
DataFetcher — NSE data via curl_cffi (Chrome TLS/JA3 fingerprint spoofing)
===========================================================================

Plain httpx/requests-ல் NSE block ஆகும் காரணம்:
  NSE Cloudflare-லிருந்து TLS fingerprint (JA3 hash) பார்க்கிறது.
  Python-ன் default ssl handshake, Chrome-ஓட் match ஆகாது → bot detect.

curl_cffi என்ன செய்கிறது:
  libcurl + BoringSSL கொண்டு real Chrome browser-ன் TLS ClientHello,
  cipher suites, extension order, JA3 hash அனைத்தையும் replicate செய்கிறது.
  Network handshake level-ல் Chrome-ஆக தெரியும் — plain User-Agent spoof-ஐ
  விட fundamentally வேற level.

Angel One / Zerodha fallbacks → httpx (SmartAPI uses its own auth, no TLS issue).

FIXES (2026-08-08):
  - get_market_breadth(): equity-stockIndices 404 → allIndices directly
  - get_volatility():     "INDIA VIX" exact match → "VIX" in sym (partial)
  - get_fii_dii():        403 graceful → source="unavailable"
  - __init__:             _breadth_primary_dead_until circuit breaker removed
"""

import asyncio
import json
import time
import random
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from curl_cffi.requests import AsyncSession, BrowserType
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.config import settings
from app.utils.helpers import clean_nse_response, safe_float, safe_int, is_market_hours_ist
from app.utils.cache import async_cache
from app.utils import health_metrics
from app.exceptions import MarketDataError

logger = logging.getLogger(__name__)

IMPERSONATE: str = "chrome131"
SESSION_TTL_SECONDS: int = 90

_NSE_HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ta;q=0.8",
    "Referer":         "https://www.nseindia.com/",
    "Origin":          "https://www.nseindia.com",
    "DNT":             "1",
    "Connection":      "keep-alive",
    "Cache-Control":   "no-cache",
    "Pragma":          "no-cache",
}


class NSEBlockedError(MarketDataError):
    """401/403 after session refresh — IP-blocked or hard-banned."""
    pass


class NSETransientError(MarketDataError):
    """429/5xx — retry-able."""
    pass


class DataFetcher:
    BASE_URL = "https://www.nseindia.com/api"
    HOME_URL  = "https://www.nseindia.com/"

    def __init__(self):
        self._session: Optional[AsyncSession] = None
        self._session_ts: float = 0.0
        self._session_lock = asyncio.Lock()
        # NOTE: _breadth_primary_dead_until removed — equity-stockIndices
        # endpoint permanently 404 on NSE, no point circuit-breaking it.

    def _make_session(self) -> AsyncSession:
        return AsyncSession(
            impersonate=IMPERSONATE,
            headers=_NSE_HEADERS,
            timeout=15,
            verify=True,
            allow_redirects=True,
            max_redirects=10,
        )

    # ── Session / Cookie management ────────────────────────────────────────

    async def _ensure_session(self, force: bool = False) -> None:
        now = time.time()
        if not force and self._session and (now - self._session_ts) < SESSION_TTL_SECONDS:
            return

        async with self._session_lock:
            if not force and self._session and (time.time() - self._session_ts) < SESSION_TTL_SECONDS:
                return

            if self._session:
                try:
                    await self._session.close()
                except Exception:
                    pass

            self._session = self._make_session()

            try:
                r1 = await self._session.get(self.HOME_URL)
                if r1.status_code >= 400:
                    raise NSEBlockedError(f"NSE homepage returned {r1.status_code}")

                await asyncio.sleep(random.uniform(0.8, 1.5))

                await self._session.get(
                    "https://www.nseindia.com/market-data/live-equity-market",
                    headers={"Referer": self.HOME_URL}
                )

                self._session_ts = time.time()
                logger.info(
                    f"NSE session refreshed via curl_cffi ({IMPERSONATE}) — "
                    f"cookies: {list(self._session.cookies.keys())}"
                )

            except NSEBlockedError:
                raise
            except Exception as e:
                raise NSEBlockedError(f"NSE session init failed: {e}")

    # ── Core GET with retry ────────────────────────────────────────────────

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.6, min=0.5, max=6),
        retry=retry_if_exception_type(NSETransientError),
    )
    async def _get(
        self,
        endpoint: str,
        params: dict = None,
        _retried_after_block: bool = False,
    ) -> dict:
        await self._ensure_session()

        url = f"{self.BASE_URL}/{endpoint}"
        try:
            resp = await self._session.get(url, params=params or {})
        except Exception as e:
            health_metrics.record("nse", "transient_error")
            raise NSETransientError(f"Transport error on {endpoint}: {e}")

        status = resp.status_code

        if status in (401, 403):
            if not _retried_after_block:
                logger.warning(
                    f"NSE {status} on {endpoint} — refreshing session and retrying once"
                )
                await self._ensure_session(force=True)
                return await self._get(endpoint, params=params, _retried_after_block=True)
            health_metrics.record("nse", "blocked")
            raise NSEBlockedError(
                f"NSE blocked {endpoint} (HTTP {status}) even after session refresh."
            )

        if status == 429 or status >= 500:
            health_metrics.record("nse", "transient_error")
            raise NSETransientError(f"NSE transient {status} on {endpoint}")

        if status >= 400:
            health_metrics.record("nse", "other_error")
            raise MarketDataError(f"NSE {status} on {endpoint}")

        try:
            data = resp.json()
        except Exception:
            text = resp.text.strip()
            if text.startswith("{") or text.startswith("["):
                data = json.loads(text)
            else:
                health_metrics.record("nse", "other_error")
                raise MarketDataError(f"Non-JSON response from {endpoint}: {text[:200]}")

        health_metrics.record("nse", "ok")
        return clean_nse_response(data) if isinstance(data, dict) else data

    # ── Angel One helpers ─────────────────────────────────────────────────

    async def _try_angel_spot(self, symbol: str) -> Optional[Dict]:
        try:
            from app.services.angel_one import angel_session
        except Exception as e:
            logger.debug(f"Angel One module unavailable: {e}")
            return None

        if not angel_session.is_configured:
            return None

        try:
            ltp = await angel_session.get_ltp(symbol)
        except Exception as e:
            health_metrics.record("angel_one", "other_error")
            logger.warning(f"Angel One get_spot failed for {symbol}, falling back to NSE: {e}")
            return None

        health_metrics.record("angel_one", "ok")
        prev_close = safe_float(ltp.get("close", 0))
        return {
            "symbol":               symbol.upper(),
            "price":                safe_float(ltp.get("price")),
            "change":               safe_float(ltp.get("change")),
            "change_percent":       safe_float(ltp.get("change_percent")),
            "high":                 safe_float(ltp.get("high")),
            "low":                  safe_float(ltp.get("low")),
            "open":                 safe_float(ltp.get("open")),
            "prev_close":           prev_close,
            "volume":               0,
            "market_open":          is_market_hours_ist(),
            "market_status_source": "estimated_ist_hours",
            "data_source":          "angel_one",
            "timestamp":            ltp.get("timestamp", datetime.now().isoformat()),
        }

    async def _try_angel_option_chain(self, symbol: str, expiry: Optional[str]) -> Optional[Dict]:
        try:
            from app.services.angel_one import angel_session
        except Exception as e:
            logger.debug(f"Angel One module unavailable: {e}")
            return None

        if not angel_session.is_configured:
            return None

        try:
            raw = await angel_session.get_option_chain(symbol, expiry or "")
        except Exception as e:
            health_metrics.record("angel_one", "other_error")
            logger.warning(f"Angel One option chain failed for {symbol}, falling back to NSE: {e}")
            return None
        health_metrics.record("angel_one", "ok")

        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not rows:
            logger.warning(f"Angel One option chain returned no rows for {symbol}, falling back to NSE")
            return None

        first = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(first, dict) or "strikePrice" not in first or not ("CE" in first or "PE" in first):
            logger.warning(
                f"Angel One option chain for {symbol} has unexpected shape — falling back to NSE"
            )
            return None

        return {
            "symbol":           symbol.upper(),
            "expiry":           expiry or raw.get("expiry", "") if isinstance(raw, dict) else "",
            "all_expiries":     sorted(
                raw.get("all_expiries", []) if isinstance(raw, dict) else [],
                key=lambda e: next(
                    (datetime.strptime(e, f).date()
                     for f in ("%d-%b-%Y", "%d%b%Y", "%d-%b-%y", "%Y-%m-%d")),
                    datetime.max.date()
                )
            ),
            "underlying_price": safe_float(raw.get("underlying_price", 0)) if isinstance(raw, dict) else 0,
            "data":             rows,
            "data_source":      "angel_one",
        }

    async def _try_angel_historical(self, symbol: str, days: int = 60) -> Optional[Dict]:
        try:
            from app.services.angel_one import angel_session
        except Exception:
            return None
        if not angel_session.is_configured:
            return None
        try:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 09:15")
            to_date   = datetime.now().strftime("%Y-%m-%d %H:%M")
            candles = None
            for attempt in range(3):
                try:
                    candles = await angel_session.get_candle_data(
                        symbol, interval="ONE_DAY", from_date=from_date, to_date=to_date
                    )
                    break
                except Exception as e:
                    err = str(e).lower()
                    if any(x in err for x in ("too many", "ab1021", "rate", "access rate", "exceeding")):
                        wait = (attempt + 1) * 3.0
                        logger.warning(f"Angel One rate limit for {symbol} — waiting {wait}s (attempt {attempt+1}/3)")
                        await asyncio.sleep(wait)
                    else:
                        raise
            if not candles:
                return None
            closes  = [c["close"] for c in candles if c.get("close")]
            volumes = [safe_int(c.get("volume", 0)) for c in candles if c.get("close")]
            if len(closes) < 5:
                return None
            logger.info(f"Historical prices for {symbol} via Angel One — {len(closes)} candles")

            if not any(volumes):
                try:
                    fut_candles = await angel_session.get_futures_candle_data(
                        symbol, interval="ONE_DAY", from_date=from_date, to_date=to_date
                    )
                    fut_volumes = [safe_int(c.get("volume", 0)) for c in fut_candles if c.get("close")]
                    if fut_volumes and any(fut_volumes) and len(fut_volumes) == len(closes):
                        volumes = fut_volumes
                        logger.info(f"Using {symbol} futures volume as proxy — {len(volumes)} bars")
                    else:
                        logger.debug(f"Futures volume proxy for {symbol} unusable")
                except Exception as e:
                    logger.debug(f"Futures volume proxy unavailable for {symbol}: {e}")

            health_metrics.record("angel_one", "ok")
            return {"closes": closes, "volumes": volumes}
        except Exception as e:
            health_metrics.record("angel_one", "other_error")
            logger.warning(f"Angel One historical failed for {symbol}: {e}")
            return None

    async def _try_zerodha_spot(self, symbol: str) -> Optional[Dict]:
        try:
            from app.services.zerodha import kite_session
        except Exception as e:
            logger.debug(f"Zerodha module unavailable: {e}")
            return None
        if not kite_session.is_configured:
            return None
        try:
            ltp = await kite_session.get_ltp(symbol)
        except Exception as e:
            health_metrics.record("zerodha", "other_error")
            logger.warning(f"Zerodha get_spot failed for {symbol}, falling back to NSE: {e}")
            return None
        health_metrics.record("zerodha", "ok")
        return {
            "symbol":               symbol.upper(),
            "price":                safe_float(ltp.get("price")),
            "change":               safe_float(ltp.get("change")),
            "change_percent":       safe_float(ltp.get("change_percent")),
            "high":                 safe_float(ltp.get("high")),
            "low":                  safe_float(ltp.get("low")),
            "open":                 safe_float(ltp.get("open")),
            "prev_close":           safe_float(ltp.get("close")),
            "volume":               0,
            "market_open":          is_market_hours_ist(),
            "market_status_source": "estimated_ist_hours",
            "data_source":          "zerodha",
            "timestamp":            ltp.get("timestamp", datetime.now().isoformat()),
        }

    async def _try_zerodha_option_chain(self, symbol: str, expiry: Optional[str]) -> Optional[Dict]:
        try:
            from app.services.zerodha import kite_session
        except Exception as e:
            logger.debug(f"Zerodha module unavailable: {e}")
            return None
        if not kite_session.is_configured:
            return None
        try:
            raw = await kite_session.get_option_chain(symbol, expiry or "")
        except Exception as e:
            health_metrics.record("zerodha", "other_error")
            logger.warning(f"Zerodha option chain failed for {symbol}, falling back to NSE: {e}")
            return None
        health_metrics.record("zerodha", "ok")

        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not rows:
            logger.warning(f"Zerodha option chain returned no rows for {symbol}, falling back to NSE")
            return None
        first = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(first, dict) or "strikePrice" not in first or not ("CE" in first or "PE" in first):
            logger.warning(f"Zerodha option chain for {symbol} has unexpected shape — falling back to NSE")
            return None

        return {
            "symbol":           symbol.upper(),
            "expiry":           expiry or raw.get("expiry", "") if isinstance(raw, dict) else "",
            "all_expiries":     raw.get("all_expiries", []) if isinstance(raw, dict) else [],
            "underlying_price": safe_float(raw.get("underlying_price", 0)) if isinstance(raw, dict) else 0,
            "data":             rows,
            "data_source":      "zerodha",
        }

    async def _try_zerodha_historical(self, symbol: str, days: int = 60) -> Optional[Dict]:
        try:
            from app.services.zerodha import kite_session
        except Exception:
            return None
        if not kite_session.is_configured:
            return None
        try:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            to_date   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            candles = await kite_session.get_candle_data(
                symbol, interval="day", from_date=from_date, to_date=to_date
            )
            if not candles:
                return None
            closes  = [c["close"] for c in candles if c.get("close")]
            volumes = [safe_int(c.get("volume", 0)) for c in candles if c.get("close")]
            if len(closes) < 5:
                return None
            logger.info(f"Historical prices for {symbol} via Zerodha — {len(closes)} candles")
            health_metrics.record("zerodha", "ok")
            return {"closes": closes, "volumes": volumes}
        except Exception as e:
            health_metrics.record("zerodha", "other_error")
            logger.warning(f"Zerodha historical failed for {symbol}: {e}")
            return None

    # ── Public API ─────────────────────────────────────────────────────────

    @async_cache(ttl=60)
    async def get_intraday_ohlc(self, symbol: str, interval: str = "FIVE_MINUTE", bars: int = 100) -> Dict:
        """
        Real intraday OHLCV candles for `symbol` — Angel One first, Zerodha
        second (spec §5 provider abstraction; CODE_REVIEW.md #15). This is
        what multi-timeframe analysis (5m/15m/1h) needs — previously the app
        only ever fetched ONE_DAY candles and slapped "5min"/"15min"/"1hr"
        labels on AI-guessed trends derived from that same daily data, which
        had no real intraday basis.

        Returns {"available": False} (never raises) when neither provider is
        configured or both fail, so callers can fall back honestly instead
        of fabricating a timeframe reading.
        """
        result = await self._angel_intraday_ohlc(symbol, interval, bars)
        if result.get("available"):
            return result
        return await self._zerodha_intraday_ohlc(symbol, interval, bars)

    async def _angel_intraday_ohlc(self, symbol: str, interval: str, bars: int) -> Dict:
        """
        Volume: NSE index candles usually report 0 volume (indices don't
        trade directly) — when that happens we swap in the front-month
        futures candle volumes for the same window as a proxy, same pattern
        already used by _try_angel_historical for the daily series.
        """
        try:
            from app.services.angel_one import angel_session
        except Exception:
            return {"available": False, "reason": "angel_one module unavailable"}
        if not angel_session.is_configured:
            return {"available": False, "reason": "Angel One not configured"}

        interval = interval.upper()
        span_days = {
            "ONE_MINUTE": 2, "THREE_MINUTE": 3, "FIVE_MINUTE": 5,
            "FIFTEEN_MINUTE": 10, "THIRTY_MINUTE": 15, "ONE_HOUR": 20,
            "ONE_DAY": 90,
        }.get(interval, 5)

        to_date = datetime.now()
        from_date = to_date - timedelta(days=span_days)
        from_str = from_date.strftime("%Y-%m-%d 09:15")
        to_str   = to_date.strftime("%Y-%m-%d %H:%M")

        # BUG FIX (2026-08-17): this previously made exactly ONE attempt and
        # gave up on any error, including Angel One's "Access denied because
        # of exceeding access rate". get_intraday_ohlc() fans out to 3
        # intervals x 3 symbols back-to-back (history_collector + dashboard
        # loads), which reliably bursts past Angel One's per-second limit.
        # _try_angel_historical() already had a 3-attempt backoff for this
        # exact error family — reusing the same pattern here so intraday
        # candles recover instead of immediately reporting "unavailable".
        candles = None
        for attempt in range(3):
            try:
                candles = await angel_session.get_candle_data(
                    symbol, interval=interval, from_date=from_str, to_date=to_str
                )
                break
            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ("too many", "ab1021", "rate", "access rate", "exceeding")):
                    wait = (attempt + 1) * 3.0
                    logger.warning(
                        f"Angel One rate limit for {symbol} {interval} — "
                        f"waiting {wait}s (attempt {attempt+1}/3)"
                    )
                    await asyncio.sleep(wait)
                else:
                    # Not a rate-limit error — retrying won't help, fail now.
                    logger.warning(f"Intraday OHLC ({interval}) fetch failed for {symbol} via Angel One: {e}")
                    return {"available": False, "reason": str(e)}
        if candles is None:
            # Exhausted all 3 rate-limit retries without success.
            return {"available": False, "reason": "Angel One rate limit — exhausted retries"}
        if not candles:
            return {"available": False, "reason": "no candles returned"}

        candles = candles[-bars:]
        highs   = [safe_float(c.get("high"))  for c in candles]
        lows    = [safe_float(c.get("low"))   for c in candles]
        closes  = [safe_float(c.get("close")) for c in candles]
        volumes = [safe_int(c.get("volume", 0)) for c in candles]
        timestamps = [c.get("timestamp") or c.get("date") or "" for c in candles]

        if not any(volumes):
            try:
                fut_candles = await angel_session.get_futures_candle_data(
                    symbol, interval=interval, from_date=from_str, to_date=to_str
                )
                fut_candles = fut_candles[-bars:]
                fut_volumes = [safe_int(c.get("volume", 0)) for c in fut_candles]
                if fut_volumes and any(fut_volumes) and len(fut_volumes) == len(closes):
                    volumes = fut_volumes
            except Exception as e:
                logger.debug(f"Futures volume proxy unavailable for {symbol} {interval}: {e}")

        return {
            "available": True,
            "interval": interval,
            "highs": highs, "lows": lows, "closes": closes, "volumes": volumes,
            "timestamps": timestamps,
            "bar_count": len(closes),
            "data_source": "angel_one_intraday",
        }

    _ZERODHA_INTERVAL_MAP = {
        "ONE_MINUTE": "minute", "FIVE_MINUTE": "5minute", "FIFTEEN_MINUTE": "15minute",
        "THIRTY_MINUTE": "30minute", "ONE_HOUR": "60minute", "ONE_DAY": "day",
    }

    async def _zerodha_intraday_ohlc(self, symbol: str, interval: str, bars: int) -> Dict:
        try:
            from app.services.zerodha import kite_session
        except Exception:
            return {"available": False, "reason": "zerodha module unavailable"}
        if not kite_session.is_configured:
            return {"available": False, "reason": "Zerodha not configured"}

        interval = interval.upper()
        kite_interval = self._ZERODHA_INTERVAL_MAP.get(interval, "5minute")
        span_days = {
            "ONE_MINUTE": 2, "FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 10,
            "THIRTY_MINUTE": 15, "ONE_HOUR": 20, "ONE_DAY": 90,
        }.get(interval, 5)

        to_date = datetime.now()
        from_date = to_date - timedelta(days=span_days)

        try:
            candles = await kite_session.get_candle_data(
                symbol, interval=kite_interval,
                from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception as e:
            logger.warning(f"Intraday OHLC ({interval}) fetch failed for {symbol} via Zerodha: {e}")
            health_metrics.record("zerodha", "other_error")
            return {"available": False, "reason": str(e)}
        if not candles:
            return {"available": False, "reason": "no candles returned"}

        health_metrics.record("zerodha", "ok")
        candles = candles[-bars:]
        return {
            "available": True,
            "interval": interval,
            "highs":   [safe_float(c.get("high"))  for c in candles],
            "lows":    [safe_float(c.get("low"))   for c in candles],
            "closes":  [safe_float(c.get("close")) for c in candles],
            "volumes": [safe_int(c.get("volume", 0)) for c in candles],
            "timestamps": [c.get("timestamp", "") for c in candles],
            "bar_count": len(candles),
            "data_source": "zerodha_intraday",
        }

    @async_cache(ttl=15)
    async def get_futures_premium(self, symbol: str) -> Dict:
        """
        Real futures premium/discount = front-month NFO futures LTP - spot,
        via Angel One. Previously this was hardcoded to 0.0 everywhere,
        which the decision engine and Tamil explainer both read as "neutral
        premium" when it actually meant "no data at all".

        Fetches spot internally (via get_spot, which is itself TTL-cached)
        rather than taking it as a parameter — this method is called
        concurrently with the main spot fetch in MarketAnalyzer, before
        that result is available.

        Returns {"status": "unavailable", ...} (never raises) when Angel One
        isn't configured, so callers can show "N/A — not connected" instead
        of a fabricated neutral 0.0.
        """
        try:
            from app.services.angel_one import angel_session
        except Exception:
            return {"status": "unavailable", "premium": 0.0, "premium_pct": 0.0}
        if not angel_session.is_configured:
            return {"status": "unavailable", "premium": 0.0, "premium_pct": 0.0}
        try:
            spot = await self.get_spot(symbol)
            spot_price = safe_float(spot.get("price", 0))
        except Exception as e:
            logger.warning(f"Spot fetch for futures-premium calc failed for {symbol}: {e}")
            return {"status": "unavailable", "premium": 0.0, "premium_pct": 0.0}
        if spot_price <= 0:
            return {"status": "unavailable", "premium": 0.0, "premium_pct": 0.0}
        try:
            fut = await angel_session.get_futures_ltp(symbol)
        except Exception as e:
            logger.warning(f"Futures premium fetch failed for {symbol}: {e}")
            return {"status": "unavailable", "premium": 0.0, "premium_pct": 0.0}
        if not fut:
            return {"status": "unavailable", "premium": 0.0, "premium_pct": 0.0}

        premium = round(fut["ltp"] - spot_price, 2)
        premium_pct = round((premium / spot_price) * 100, 3) if spot_price > 0 else 0.0
        return {
            "status": "live",
            "premium": premium,
            "premium_pct": premium_pct,
            "futures_ltp": fut["ltp"],
            "futures_expiry": fut.get("expiry", ""),
        }

    @async_cache(ttl=1800)
    async def get_historical_prices(self, symbol: str, days: int = 60) -> Dict:
        angel_hist = await self._try_angel_historical(symbol, days)
        if angel_hist:
            return angel_hist

        zerodha_hist = await self._try_zerodha_historical(symbol, days)
        if zerodha_hist:
            return zerodha_hist

        index_map = {
            "NIFTY":     "NIFTY 50",
            "BANKNIFTY": "BANK NIFTY",
            "FINNIFTY":  "FINNIFTY",
        }
        index_name = index_map.get(symbol.upper())
        if not index_name:
            raise ValueError(f"Unsupported symbol: {symbol}")

        to_date   = datetime.now().date()
        from_date = to_date - timedelta(days=days)
        params = {
            "indexType": index_name,
            "from": from_date.strftime("%d-%m-%Y"),
            "to":   to_date.strftime("%d-%m-%Y"),
        }
        raw = await self._get("historicalOR/indicesHistory", params=params)

        rows = None
        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict):
            block = raw.get("data")
            if isinstance(block, list):
                rows = block
            elif isinstance(block, dict):
                for key in ("indexCloseOnlineRecords", "indexCloseOnline", "close", "indexData"):
                    candidate = block.get(key)
                    if isinstance(candidate, list) and candidate:
                        rows = candidate
                        break
                if not rows:
                    inner = block.get("data")
                    if isinstance(inner, list) and inner:
                        rows = inner

        if not rows:
            if isinstance(raw, dict):
                top_keys = list(raw.keys())
                block = raw.get("data")
                inner_keys = list(block.keys()) if isinstance(block, dict) else type(block).__name__
                logger.debug(
                    f"get_historical_prices({symbol}): unrecognised shape — "
                    f"top keys={top_keys}, data type/keys={inner_keys}"
                )
            raise MarketDataError("Unexpected NSE historical response shape")

        parsed = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cv = row.get("EOD_CLOSE_INDEX_VAL") or row.get("CLOSE") or row.get("close")
            ts = row.get("EOD_TIMESTAMP") or row.get("TIMESTAMP") or row.get("timestamp")
            if cv is None or ts is None:
                continue
            dt = None
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    dt = datetime.strptime(str(ts).strip(), fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                continue
            parsed.append((dt, safe_float(cv)))

        if not parsed:
            raise MarketDataError("NSE historical response had no parseable rows")

        parsed.sort(key=lambda r: r[0])
        return {"closes": [c for _, c in parsed], "volumes": []}

    @async_cache(ttl=10)
    async def get_spot(self, symbol: str) -> Dict:
        angel_result = await self._try_angel_spot(symbol)
        if angel_result is not None:
            return angel_result

        zerodha_result = await self._try_zerodha_spot(symbol)
        if zerodha_result is not None:
            return zerodha_result

        index_map = {
            "NIFTY":     "NIFTY 50",
            "BANKNIFTY": "BANK NIFTY",
            "FINNIFTY":  "FINNIFTY",
        }
        index_name = index_map.get(symbol.upper())
        if not index_name:
            raise ValueError(f"Unsupported symbol: {symbol}")

        data = await self._get("equity-stockIndices", params={"index": index_name})
        if not data or "data" not in data or not data["data"]:
            raise MarketDataError(f"No spot data for {symbol}")

        item   = data["data"][0]
        ms     = data.get("marketStatus", {})
        ms_str = ms.get("marketStatus") if isinstance(ms, dict) else None
        is_open = (ms_str.lower() == "open") if ms_str else is_market_hours_ist()

        return {
            "symbol":               symbol.upper(),
            "price":                safe_float(item.get("lastPrice")),
            "change":               safe_float(item.get("change")),
            "change_percent":       safe_float(item.get("pChange")),
            "high":                 safe_float(item.get("dayHigh")),
            "low":                  safe_float(item.get("dayLow")),
            "open":                 safe_float(item.get("open", item.get("dayOpen", 0))),
            "prev_close":           safe_float(item.get("previousClose", item.get("prevClose", 0))),
            "volume":               safe_int(item.get("totalTradedVolume")),
            "market_open":          is_open,
            "market_status_source": "nse" if ms_str else "estimated_ist_hours",
            "data_source":          f"nse_curl_cffi/{IMPERSONATE}",
            "timestamp":            datetime.now().isoformat(),
        }

    @async_cache(ttl=30)
    async def get_option_chain(self, symbol: str, expiry: Optional[str] = None) -> Dict:
        sym = symbol.upper()
        if sym not in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            raise ValueError(f"Unsupported symbol: {symbol}")

        angel_result = await self._try_angel_option_chain(sym, expiry)
        if angel_result is not None:
            return angel_result

        zerodha_result = await self._try_zerodha_option_chain(sym, expiry)
        if zerodha_result is not None:
            return zerodha_result

        raw = await self._get("option-chain-indices", params={"symbol": sym})
        records = (raw or {}).get("records")
        if not records:
            raise MarketDataError("NSE option-chain: missing 'records' block")

        expiry_list = records.get("expiryDates", [])
        if not expiry_list:
            raise MarketDataError("NSE option-chain: no expiry dates")

        if not expiry:
            today = datetime.now().date()
            future = []
            for e in expiry_list:
                for fmt in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(e, fmt).date()
                        if dt >= today:
                            future.append((dt, e))
                        break
                    except ValueError:
                        continue
            if not future:
                raise MarketDataError("No future expiry found in NSE option chain")
            future.sort()
            expiry = future[0][1]

        all_strikes = records.get("data", [])
        chain_rows  = [r for r in all_strikes if r.get("expiryDate") == expiry]
        if not chain_rows:
            raise MarketDataError(f"No option chain rows for expiry {expiry}")

        def _parse_expiry_date(e):
            import re as _re
            e2 = e.strip()
            # Handle 01SEP2026, 01Sep2026, 01-Sep-2026, 2026-09-01
            m = _re.match(r"(\d{1,2})[-]?([A-Za-z]{3})[-]?(\d{4})", e2)
            if m:
                try:
                    return datetime.strptime(f"{m.group(1)}{m.group(2).title()}{m.group(3)}", "%d%b%Y").date()
                except Exception:
                    pass
            for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
                try:
                    return datetime.strptime(e2, fmt).date()
                except ValueError:
                    continue
            return datetime.max.date()
        sorted_expiries = sorted(expiry_list, key=_parse_expiry_date)
        return {
            "symbol":           sym,
            "expiry":           expiry,
            "all_expiries":     sorted_expiries,
            "underlying_price": safe_float(records.get("underlyingValue", 0)),
            "data":             chain_rows,
            "data_source":      f"nse_curl_cffi/{IMPERSONATE}",
        }

    async def get_spot_multiple(self, symbols: List[str]) -> Dict:
        results = await asyncio.gather(
            *[self.get_spot(s) for s in symbols],
            return_exceptions=True
        )
        return {
            sym: (None if isinstance(res, Exception) else res)
            for sym, res in zip(symbols, results)
        }

    @async_cache(ttl=30)
    async def get_volatility(self) -> float:
        """India VIX — Angel One first, allIndices fallback.

        FIX: "INDIA VIX" exact match → "VIX" in sym (partial/case-insensitive)
        NSE allIndices-ல் indexSymbol "India VIX" ஆக வரும் — exact upper()
        match miss ஆகும். "VIX" in sym check reliable.
        """
        try:
            from app.services.angel_one import angel_session
            if angel_session is not None and angel_session.is_configured:
                try:
                    vix = await angel_session.get_india_vix()
                    if vix:
                        return vix
                except Exception as e:
                    logger.warning(f"India VIX via Angel One failed: {e}")
        except Exception:
            pass

        try:
            data = await self._get("allIndices")
            items = (data or {}).get("data", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                # FIX: partial match instead of exact "INDIA VIX"
                sym = item.get("indexSymbol", "").upper()
                if "VIX" in sym:
                    val = safe_float(item.get("last", item.get("lastPrice", 0)))
                    if val:
                        logger.info(f"India VIX from allIndices ({sym}): {val}")
                        return val
            logger.warning("VIX: not found in allIndices response")
        except Exception as e:
            logger.error(f"VIX fetch error: {e}")

        return 0.0

    @async_cache(ttl=30)
    async def get_market_breadth(self) -> Dict:
        """NIFTY 50 advance/decline.

        FIX: equity-stockIndices NSE 404 (endpoint deprecated) — removed.
        Now directly uses allIndices which is already called for VIX/sectors.
        One endpoint, one call, cached 30s — no more repeated 404 log lines.
        """
        try:
            data = await self._get("allIndices")
            items = (data or {}).get("data", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                sym = item.get("indexSymbol", "")
                # "NIFTY 50" match, exclude "NIFTY BANK", "NIFTY 500" etc.
                if sym.strip().upper() == "NIFTY 50":
                    adv = item.get("advance", {})
                    if adv and (adv.get("advances") or adv.get("declines")):
                        return {
                            "advances":  safe_int(adv.get("advances",  0)),
                            "declines":  safe_int(adv.get("declines",  0)),
                            "unchanged": safe_int(adv.get("unchanged", 0)),
                            "source":    "nse_allIndices",
                        }
            logger.warning("Market breadth: NIFTY 50 advance/decline not in allIndices")
        except Exception as e:
            logger.error(f"Market breadth error: {e}")

        return {"advances": 0, "declines": 0, "unchanged": 0, "source": "unavailable"}

    @async_cache(ttl=1800)
    async def get_fii_dii(self) -> Dict:
        """FII/DII — NSE fiidiiTradeReact.

        NSE Render server-ல் 403 return பண்றது (Cloudflare IP block).
        Graceful fallback — source="unavailable", fake numbers இல்லை.
        """
        try:
            raw = await self._get("fiidiiTradeReact")
            rows = raw if isinstance(raw, list) else (raw or {}).get("data", [])
            if rows:
                out = {"date": None, "fii": None, "dii": None, "source": "nse"}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    category = str(row.get("category", "")).strip().upper()
                    entry = {
                        "buy_value":  safe_float(row.get("buyValue")),
                        "sell_value": safe_float(row.get("sellValue")),
                        "net_value":  safe_float(row.get("netValue")),
                    }
                    out["date"] = row.get("date", out["date"])
                    if category.startswith("FII") or category.startswith("FPI"):
                        out["fii"] = entry
                    elif category.startswith("DII"):
                        out["dii"] = entry
                if out["fii"] is not None or out["dii"] is not None:
                    return out
        except Exception as e:
            logger.warning(f"FII/DII fetch failed: {e}")

        return {"date": None, "fii": None, "dii": None, "source": "unavailable"}

    _SECTOR_INDEX_MAP = {
        "NIFTY BANK":               "Nifty Bank",
        "NIFTY IT":                 "Nifty IT",
        "NIFTY AUTO":               "Nifty Auto",
        "NIFTY PHARMA":             "Nifty Pharma",
        "NIFTY PSU BANK":           "Nifty PSU Bank",
        "NIFTY FMCG":               "Nifty FMCG",
        "NIFTY METAL":              "Nifty Metal",
        "NIFTY FINANCIAL SERVICES": "Nifty Financial Services",
        "NIFTY REALTY":             "Nifty Realty",
        "NIFTY ENERGY":             "Nifty Energy",
    }

    @async_cache(ttl=60)
    async def get_sector_performance(self) -> Dict:
        try:
            data = await self._get("allIndices")
        except Exception as e:
            logger.warning(f"Sector performance fetch failed: {e}")
            return {"sectors": [], "top_sector": None, "weak_sector": None,
                    "rotation": "unavailable", "source": "unavailable"}

        items = (data or {}).get("data", [])
        sectors = []
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = item.get("indexSymbol", "").strip().upper()
            display = self._SECTOR_INDEX_MAP.get(symbol)
            if not display:
                continue
            chg_pct = safe_float(item.get("percentChange", item.get("pChange", 0)))
            sectors.append({
                "name":           display,
                "index_symbol":   symbol,
                "last":           safe_float(item.get("last", item.get("lastPrice", 0))),
                "change_percent": chg_pct,
                "strength_score": round(max(0.0, min(100.0, 50 + (chg_pct / 3.0) * 50)), 1),
            })

        if not sectors:
            return {"sectors": [], "top_sector": None, "weak_sector": None,
                    "rotation": "unavailable", "source": "unavailable"}

        sectors.sort(key=lambda s: s["change_percent"], reverse=True)
        top_sector  = sectors[0]
        weak_sector = sectors[-1]
        advancing   = sum(1 for s in sectors if s["change_percent"] > 0)
        declining   = sum(1 for s in sectors if s["change_percent"] < 0)

        if advancing >= 7:
            rotation = "Broad-based Buying"
        elif declining >= 7:
            rotation = "Broad-based Selling"
        elif top_sector["change_percent"] - weak_sector["change_percent"] > 2:
            rotation = f"Rotation into {top_sector['name']}, out of {weak_sector['name']}"
        else:
            rotation = "Mixed / No Clear Rotation"

        return {
            "sectors":     sectors,
            "top_sector":  top_sector,
            "weak_sector": weak_sector,
            "advancing":   advancing,
            "declining":   declining,
            "rotation":    rotation,
            "source":      "nse",
        }

    async def close(self) -> None:
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None