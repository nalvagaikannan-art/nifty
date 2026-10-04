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
import re
import time
import random
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional

from curl_cffi.requests import AsyncSession, BrowserType
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.config import settings
from app.utils.helpers import clean_nse_response, safe_float, safe_int, is_market_hours_ist, now_ist as current_ist
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


def _parse_expiry_date(e: str):
    """Robust NSE/Angel-One expiry string parser — handles 01SEP2026,
    01Sep2026, 01-Sep-2026, 2026-09-01, dd/mm/yyyy. Never raises; returns
    datetime.max.date() (sorts last) on anything unparseable.

    BUG FIX (2026-08-17): the sort key inside _try_angel_option_chain used
    to be `next((datetime.strptime(e, f).date() for f in (...)), default)`.
    That pattern is broken — datetime.strptime() raises ValueError on a
    mismatched format, and a generator expression does NOT catch that and
    move on to the next format; the exception propagates straight out of
    next()/sorted(), uncaught. Angel One returns dateless-separator strings
    like '01SEP2026', and the first format tried was the dashed
    '%d-%b-%Y', so every call failed with exactly the error seen in
    production: "time data '01SEP2026' does not match format '%d-%b-%Y'".
    This single always-safe helper replaces every ad-hoc expiry parser in
    this module (get_option_chain had its own near-identical inner
    function — consolidated here so there's one parser to fix, not two).
    """
    if not e:
        return datetime.max.date()
    e2 = e.strip()
    m = re.match(r"(\d{1,2})[-/]?([A-Za-z]{3})[-/]?(\d{4})", e2)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}{m.group(2).title()}{m.group(3)}", "%d%b%Y").date()
        except Exception:
            pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(e2, fmt).date()
        except ValueError:
            continue
    return datetime.max.date()


class DataFetcher:
    BASE_URL = "https://www.nseindia.com/api"
    HOME_URL  = "https://www.nseindia.com/"

    def __init__(self):
        self._session: Optional[AsyncSession] = None
        self._session_ts: float = 0.0
        self._session_lock = asyncio.Lock()

        # Futures-premium cache is intentionally independent of the generic
        # async_cache() TTL.  HTTP analysis must not block every ~25s waiting
        # for an Angel One futures-LTP call.
        self._futures_premium_cache: Dict[str, Dict] = {}
        self._futures_premium_cache_ts: Dict[str, float] = {}
        self._futures_premium_refresh_tasks: Dict[str, asyncio.Task] = {}
        self._futures_premium_lock = asyncio.Lock()

        # Fresh enough for immediate use; beyond this we refresh in background.
        self._futures_premium_refresh_after = 15.0

        # Never present an excessively old futures premium as live data.
        # If older than this, the next caller performs a foreground refresh.
        self._futures_premium_hard_stale_after = 60.0

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

    async def _try_angel_live_feed_spot(self, symbol: str) -> Optional[Dict]:
        """
        Return a fresh Angel One WebSocket LTP.

        Exchange timestamp is the freshness authority. Stale or missing
        WebSocket data returns None so the existing REST/Zerodha/NSE
        fallback chain remains intact.
        """
        symbol = symbol.upper()

        if symbol not in {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}:
            return None

        try:
            from app.services.angel_live_feed import angel_live_feed
        except Exception as e:
            logger.debug("Angel live feed unavailable: %s", e)
            return None

        try:
            tick = angel_live_feed.get_tick(symbol)
        except Exception as e:
            logger.debug("Live feed tick read failed for %s: %s", symbol, e)
            return None

        if not isinstance(tick, dict):
            return None

        try:
            price = float(tick.get("ltp"))
            exchange_ts = float(tick.get("exchange_timestamp")) / 1000.0
        except (TypeError, ValueError):
            return None

        if price <= 0 or exchange_ts <= 0:
            return None

        age_sec = time.time() - exchange_ts

        if age_sec < -5:
            logger.warning(
                "Rejecting future Angel WebSocket tick for %s: age=%.1fs",
                symbol,
                age_sec,
            )
            return None

        if age_sec > 10:
            logger.debug(
                "Angel WebSocket tick stale for %s: age=%.1fs",
                symbol,
                age_sec,
            )
            return None

        logger.info(
            "Using fresh Angel WebSocket spot for %s: LTP=%.2f age=%.1fs",
            symbol,
            price,
            age_sec,
        )

        return {
            "symbol": symbol,
            "price": price,
            "change": 0.0,
            "change_percent": 0.0,
            "high": 0.0,
            "low": 0.0,
            "open": 0.0,
            "prev_close": 0.0,
            "volume": 0,
            "market_open": is_market_hours_ist(),
            "market_status_source": "angel_one_websocket",
            "data_source": "angel_one_websocket",
            "timestamp": tick.get("timestamp", current_ist().isoformat()),
            "exchange_timestamp": tick.get("exchange_timestamp"),
            "tick_age_sec": round(age_sec, 3),
        }

    async def _try_angel_spot(self, symbol: str) -> Optional[Dict]:
        try:
            from app.services.angel_one import angel_session, get_angel_call_priority
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
            "timestamp":            ltp.get("timestamp", current_ist().isoformat()),
        }

    async def _try_angel_option_chain(
        self,
        symbol: str,
        expiry: Optional[str],
        spot_price: Optional[float] = None,
        strikes_each_side: Optional[int] = 10,
    ) -> Optional[Dict]:
        try:
            from app.services.angel_one import angel_session
        except Exception as e:
            logger.debug(f"Angel One module unavailable: {e}")
            return None

        if not angel_session.is_configured:
            return None

        try:
            raw = await angel_session.get_option_chain(
                symbol,
                expiry or "",
                spot_price=spot_price,
                strikes_each_side=strikes_each_side,
            )
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
                key=_parse_expiry_date
            ),
            "underlying_price": safe_float(raw.get("underlying_price", 0)) if isinstance(raw, dict) else 0,
            # LOT_SIZE_SOURCE_OF_TRUTH_20260925
            "lot_size":         int(safe_float(raw.get("lot_size", 0))) if isinstance(raw, dict) else 0,
            "data":             rows,
            "data_source":      "angel_one",
        }

    async def _try_angel_historical(
        self,
        symbol: str,
        days: int = 60,
        include_volume_proxy: bool = True,
    ) -> Optional[Dict]:
        try:
            from app.services.angel_one import angel_session
        except Exception:
            return None
        if not angel_session.is_configured:
            return None
        try:
            from_date = (current_ist() - timedelta(days=days)).strftime("%Y-%m-%d 09:15")
            to_date   = current_ist().strftime("%Y-%m-%d %H:%M")
            try:
                candles = await angel_session.get_candle_data(
                    symbol,
                    interval="ONE_DAY",
                    from_date=from_date,
                    to_date=to_date,
                    fail_fast_rate_limit=True,
                )
            except Exception as e:
                logger.warning(
                    f"Angel historical fetch failed for {symbol}: {e}"
                )
                return None
            if not candles:
                return None
            closes  = [c["close"] for c in candles if c.get("close")]
            volumes = [safe_int(c.get("volume", 0)) for c in candles if c.get("close")]
            if len(closes) < 5:
                return None
            logger.info(f"Historical prices for {symbol} via Angel One — {len(closes)} candles")

            if include_volume_proxy and not any(volumes):
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
            "timestamp":            ltp.get("timestamp", current_ist().isoformat()),
        }

    async def _try_zerodha_option_chain(self, symbol: str, expiry: Optional[str], strikes_each_side: Optional[int] = 10) -> Optional[Dict]:
        try:
            from app.services.zerodha import kite_session
        except Exception as e:
            logger.debug(f"Zerodha module unavailable: {e}")
            return None
        if not kite_session.is_configured:
            return None
        try:
            raw = await kite_session.get_option_chain(symbol, expiry or "", strikes_each_side=strikes_each_side)
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
            from_date = (current_ist() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            to_date   = current_ist().strftime("%Y-%m-%d %H:%M:%S")
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

    # INTRADAY_LAST_GOOD_FALLBACK_20260923
    # Keep the most recent successful real intraday snapshot available even
    # when the next Angel One refresh is rejected by rate limiting.
    # Freshness is still validated later from candle timestamps, and current
    # WebSocket completed 5-minute candles are merged by MarketAnalyzer.
    _LAST_GOOD_INTRADAY_SNAPSHOTS: Dict[tuple, Dict] = {}

    # FUTURES_VOLUME_PROXY_CACHE_20260924
    # NIFTY/BANKNIFTY/FINNIFTY index candles normally have zero volume.
    # Keep futures volume as a separate best-effort cache so VWAP/volume
    # indicators can use real traded futures volume without adding a
    # foreground Angel request to every analysis.
    _FUTURES_VOLUME_CACHE: Dict[str, Dict[float, int]] = {}
    _FUTURES_VOLUME_CACHE_TS: Dict[str, float] = {}
    _FUTURES_VOLUME_TASKS: Dict[str, asyncio.Task] = {}
    _FUTURES_VOLUME_CACHE_TTL = 150.0

    # ── Public API ─────────────────────────────────────────────────────────

    # SAFETY FIX 2026-09-11:
# Keep successful real intraday candles cached for 120 seconds.
# This reduces unnecessary Angel One candle requests when strategy
# requests arrive just after the old 60-second cache boundary.
#
# IMPORTANT:
# Cache TTL is NOT the freshness check.
# _safe_multi_timeframe() recalculates candle age from timestamps
# on every analysis request. The decision engine still requires
# real + fresh 5m, 15m and 1h data before a directional signal.
#
# Broker/API failures remain cached for only 30 seconds so temporary
# failures can recover without repeatedly hitting Angel One.
    async def get_intraday_ohlc(
        self,
        symbol: str,
        interval: str = "FIVE_MINUTE",
        bars: int = 100,
    ) -> Dict:
        """
        Return the requested number of real intraday candles.

        SAFETY FIX 2026-09-11:
        Dashboard MTF requests 1200 bars while the Live chart requests 100
        bars. Caching this public function directly made those two requests
        different cache keys and could cause duplicate Angel One candle calls.

        The actual broker snapshot is now cached once by
        `_get_intraday_ohlc_snapshot()` without `bars` in its cache key.
        Each caller receives only its requested trailing window.

        IMPORTANT:
        This cache optimization does NOT change freshness validation.
        DecisionEngine still requires real + fresh 5m/15m/1h data.
        """
        # SAFETY FIX 2026-09-11:
        # Clamp the requested window to a sensible positive integer.
        # The shared snapshot contains up to 1200 candles, so no caller can
        # accidentally request an unbounded broker payload.
        try:
            requested_bars = max(1, min(int(bars), 1200))
        except (TypeError, ValueError):
            requested_bars = 100

        # Fetch ONE shared 5-minute snapshot. `bars` is intentionally absent
        # from this cached function's signature, so 100-bar and 1200-bar
        # callers reuse the same broker result.
        result = await self._get_intraday_ohlc_snapshot(symbol, interval)

        # INTRADAY_LAST_GOOD_FALLBACK_20260923
        # Do not let a temporary Angel One rate-limit turn valid MTF into
        # "unavailable" when we already have a real historical snapshot.
        snapshot_key = (str(symbol).upper(), str(interval).upper())

        if isinstance(result, dict) and result.get("available"):
            self._LAST_GOOD_INTRADAY_SNAPSHOTS[snapshot_key] = {
                "cached_at": current_ist().isoformat(),
                "result": dict(result),
            }

        elif isinstance(result, dict):
            failure_reason = result.get("reason")
            remembered = self._LAST_GOOD_INTRADAY_SNAPSHOTS.get(snapshot_key)

            if remembered and isinstance(remembered.get("result"), dict):
                restored = dict(remembered["result"])
                restored["snapshot_fallback"] = True
                restored["snapshot_fallback_reason"] = failure_reason
                restored["snapshot_cached_at"] = remembered.get("cached_at")

                logger.warning(
                    "Using last-known-good intraday snapshot for %s/%s "
                    "after current broker fetch failed: %s",
                    symbol,
                    interval,
                    failure_reason,
                )
                result = restored

        if not isinstance(result, dict) or not result.get("available"):
            return result

        # FUTURES_VOLUME_SNAPSHOT_ENRICH_20260924
        # `_get_intraday_ohlc_snapshot()` is cached for 120 seconds. A
        # background futures-volume refresh can therefore complete while the
        # cached index snapshot still contains volume=0. Enrich the cached
        # snapshot here so the new futures volume becomes visible to MTF
        # callers without another index candle request.
        # FUTURES_VOLUME_REFRESH_SCHEDULE_FIX_20260924
        if not any(result.get("volumes") or []):
            cached_futures_volumes = self._get_cached_futures_volumes(
                symbol,
                result.get("timestamps") or [],
            )

            if cached_futures_volumes is not None:
                result = dict(result)
                result["volumes"] = cached_futures_volumes
                result["volume_source"] = "front_month_futures_proxy"

                logger.info(
                    "Applied cached %s futures volume to intraday snapshot — %d bars",
                    str(symbol).upper(),
                    sum(1 for v in cached_futures_volumes if safe_int(v) > 0),
                )
            elif str(interval).upper() == "FIVE_MINUTE":
                # The index snapshot may have come from last-known-good
                # fallback after an Angel rate-limit. In that branch
                # _angel_intraday_ohlc() did not get a chance to schedule
                # the futures-volume refresh. Schedule it here instead.
                now_ist = current_ist()
                from_str = (
                    now_ist - timedelta(days=30)
                ).strftime("%Y-%m-%d 09:15")
                to_str = now_ist.strftime("%Y-%m-%d %H:%M")

                self._schedule_futures_volume_refresh(
                    symbol,
                    "FIVE_MINUTE",
                    from_str,
                    to_str,
                )

                logger.info(
                    "Scheduled futures volume proxy refresh for %s/FIVE_MINUTE",
                    str(symbol).upper(),
                )

        # Return only the trailing candles requested by this caller.
        # This keeps Live charts at 100 bars while MTF analysis can receive
        # the full 1200-bar snapshot needed to derive 15m/1h frames.
        trimmed = dict(result)

        for key in (
            "opens",
            "highs",
            "lows",
            "closes",
            "volumes",
            "timestamps",
        ):
            values = result.get(key)
            if isinstance(values, list):
                trimmed[key] = values[-requested_bars:]

        trimmed["bar_count"] = len(trimmed.get("closes") or [])
        return trimmed

    @async_cache(ttl=120, failure_ttl=120)
    async def _get_intraday_ohlc_snapshot(
        self,
        symbol: str,
        interval: str = "FIVE_MINUTE",
    ) -> Dict:
        """
        Shared broker snapshot for intraday OHLCV.

        SAFETY FIX 2026-09-11:
        Cache the full 1200-bar snapshot rather than caching each caller's
        requested `bars` value separately. This prevents Dashboard/Live
        duplicate broker calls for the same symbol + interval.

        Broker/API failures remain cached for 120 seconds.
        """
        # SAFETY FIX 2026-09-11:
        # Always fetch the maximum analysis window once. The public method
        # slices this result for smaller callers such as the Live chart.
        snapshot_bars = 1200

        result = await self._angel_intraday_ohlc(
            symbol,
            interval,
            snapshot_bars,
        )

        if result.get("available"):
            return result

        # If Angel returned a successful but stale snapshot, still try the
        # secondary broker before exposing the stale-data diagnostic.
        angel_diagnostic = result if result.get("stale") else None

        fallback = await self._zerodha_intraday_ohlc(
            symbol,
            interval,
            snapshot_bars,
        )

        if fallback.get("available"):
            return fallback

        if angel_diagnostic is not None:
            # Preserve the useful Angel freshness diagnosis instead of hiding
            # it behind a generic "Zerodha not configured" message.
            if fallback.get("reason"):
                angel_diagnostic = dict(angel_diagnostic)
                angel_diagnostic["fallback_reason"] = fallback.get("reason")
            return angel_diagnostic

        return fallback

    @staticmethod
    def _candle_timestamp_key(value) -> Optional[float]:
        """Normalize Angel candle timestamps to epoch seconds for alignment."""
        if value in (None, ""):
            return None
        try:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                ist_tz = current_ist().tzinfo
                if ist_tz is not None:
                    dt = dt.replace(tzinfo=ist_tz)
            return float(dt.timestamp())
        except Exception:
            return None

    def _get_cached_futures_volumes(
        self,
        symbol: str,
        timestamps: List,
    ) -> Optional[List[int]]:
        """Return futures-volume values aligned to index timestamps."""
        symbol = str(symbol).upper().strip()
        cached = self._FUTURES_VOLUME_CACHE.get(symbol)
        cached_ts = self._FUTURES_VOLUME_CACHE_TS.get(symbol, 0.0)

        if not cached or cached_ts <= 0:
            return None

        if (time.time() - cached_ts) > self._FUTURES_VOLUME_CACHE_TTL:
            return None

        aligned = []
        matched = 0

        for ts in timestamps:
            key = self._candle_timestamp_key(ts)
            value = cached.get(key, 0) if key is not None else 0
            value = safe_int(value)
            aligned.append(value)
            if value > 0:
                matched += 1

        if matched == 0:
            return None

        return aligned

    async def _refresh_futures_volume_proxy(
        self,
        symbol: str,
        interval: str,
        from_str: str,
        to_str: str,
    ) -> None:
        """Best-effort background refresh of front-month futures volumes."""
        symbol = str(symbol).upper().strip()
        interval = str(interval).upper().strip()

        try:
            from app.services.angel_one import angel_session
        except Exception as e:
            logger.debug(
                "Futures volume proxy import unavailable for %s: %s",
                symbol,
                e,
            )
            return

        try:
            if not angel_session.is_configured:
                return

            if symbol not in {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}:
                return

            candles = await angel_session.get_futures_candle_data(
                symbol,
                interval=interval,
                from_date=from_str,
                to_date=to_str,
            )

            if not candles:
                logger.debug(
                    "Futures volume proxy returned no candles for %s",
                    symbol,
                )
                return

            volume_map: Dict[float, int] = {}

            for candle in candles:
                if not isinstance(candle, dict):
                    continue

                ts = candle.get("timestamp") or candle.get("date")
                key = self._candle_timestamp_key(ts)
                if key is None:
                    continue

                volume = safe_int(candle.get("volume", 0))
                if volume > 0:
                    volume_map[key] = volume

            if volume_map:
                self._FUTURES_VOLUME_CACHE[symbol] = volume_map
                self._FUTURES_VOLUME_CACHE_TS[symbol] = time.time()

                logger.info(
                    "Futures volume proxy cache refreshed: %s %s — %d bars",
                    symbol,
                    interval,
                    len(volume_map),
                )
            else:
                logger.debug(
                    "Futures volume proxy had no usable volume: %s %s",
                    symbol,
                    interval,
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(
                "Futures volume proxy refresh unavailable for %s: %s",
                symbol,
                e,
            )
        finally:
            self._FUTURES_VOLUME_TASKS.pop(symbol, None)

    def _schedule_futures_volume_refresh(
        self,
        symbol: str,
        interval: str,
        from_str: str,
        to_str: str,
    ) -> None:
        """Start at most one background futures-volume refresh per symbol."""
        symbol = str(symbol).upper().strip()

        existing = self._FUTURES_VOLUME_TASKS.get(symbol)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(
            self._refresh_futures_volume_proxy(
                symbol,
                interval,
                from_str,
                to_str,
            )
        )
        self._FUTURES_VOLUME_TASKS[symbol] = task

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
            "ONE_MINUTE": 2, "THREE_MINUTE": 3, "FIVE_MINUTE": 30,
            "FIFTEEN_MINUTE": 30, "THIRTY_MINUTE": 30, "ONE_HOUR": 60,
            "ONE_DAY": 90,
        }.get(interval, 5)

        # FIX 2026-09-11:
        # current_ist() on the OCI VM is UTC.  Angel One expects
        # NSE/IST market time.  Using UTC here can produce an invalid
        # request such as fromdate=09:15 and todate=06:55 on the
        # same calendar date (from > to).
        now_ist = current_ist()
        from_date = now_ist - timedelta(days=span_days)
        to_date = now_ist

        from_str = from_date.strftime("%Y-%m-%d 09:15")
        to_str   = to_date.strftime("%Y-%m-%d %H:%M")

        # Rate-limit handling is centralized in angel_one._fetch_candles().
        # DataFetcher deliberately does not add another retry/backoff layer.
        try:
            candles = await angel_session.get_candle_data(
                symbol,
                interval=interval,
                from_date=from_str,
                to_date=to_str,
                fail_fast_rate_limit=True,
            )
        except Exception as e:
            logger.warning(
                f"Intraday OHLC ({interval}) fetch failed for "
                f"{symbol} via Angel One: {e}"
            )
            return {"available": False, "reason": str(e)}

        if not candles:
            return {"available": False, "reason": "no candles returned"}

        candles = candles[-bars:]
        opens   = [safe_float(c.get("open"))  for c in candles]
        highs   = [safe_float(c.get("high"))  for c in candles]
        lows    = [safe_float(c.get("low"))   for c in candles]
        closes  = [safe_float(c.get("close")) for c in candles]
        volumes = [safe_int(c.get("volume", 0)) for c in candles]
        timestamps = [c.get("timestamp") or c.get("date") or "" for c in candles]

        from app.services.angel_one import (
            ANGEL_PRIORITY_BACKGROUND,
            get_angel_call_priority,
        )
        # FUTURES_VOLUME_PROXY_CACHE_20260924
        # NSE index candles normally have zero traded volume.  VWAP/volume
        # indicators therefore use front-month index-futures volume as a
        # supplementary proxy.
        #
        # IMPORTANT:
        #   * Never make futures volume a prerequisite for MTF availability.
        #   * Never block the foreground OHLC request waiting for futures data.
        #   * Use a short-lived cache and at most one background refresh task.
        #
        # The first request after cache expiry may still return volume=0.
        # Once the background refresh completes, subsequent analysis requests
        # receive timestamp-aligned real futures volumes.

        if not any(volumes):
            cached_futures_volumes = self._get_cached_futures_volumes(
                symbol,
                timestamps,
            )

            if cached_futures_volumes is not None:
                volumes = cached_futures_volumes
                logger.info(
                    "Using cached %s futures volume as VWAP proxy — %d bars",
                    symbol.upper(),
                    sum(1 for v in volumes if safe_int(v) > 0),
                )

            self._schedule_futures_volume_refresh(
                symbol,
                interval,
                from_str,
                to_str,
            )

        # SAFETY FIX 2026-09-14:
        # Angel One can return HTTP/API success with an old candle snapshot.
        # `status=True` alone therefore does NOT prove that intraday data is
        # usable for the current trading session.
        #
        # During regular NSE hours, reject:
        #   1) candles from a previous calendar/trading session
        #   2) candles older than the interval-specific freshness window
        #
        # This protects the Live chart/API from displaying Sep-11 data as
        # "live" on Sep-14, while allowing the existing DecisionEngine/MTF
        # freshness gates to remain unchanged.
        latest_timestamp = timestamps[-1] if timestamps else ""

        freshness_limits_minutes = {
            "ONE_MINUTE": 5,
            "THREE_MINUTE": 10,
            "FIVE_MINUTE": 15,
            "FIFTEEN_MINUTE": 30,
            "THIRTY_MINUTE": 60,
            "ONE_HOUR": 90,
        }

        if interval in freshness_limits_minutes and latest_timestamp:
            try:
                ts_text = str(latest_timestamp).strip()

                if ts_text.endswith("Z"):
                    parsed_latest = datetime.fromisoformat(
                        ts_text[:-1] + "+00:00"
                    )
                else:
                    parsed_latest = datetime.fromisoformat(ts_text)

                if parsed_latest.tzinfo is None:
                    parsed_latest = parsed_latest.replace(
                        tzinfo=ZoneInfo("Asia/Kolkata")
                    )

                latest_ist = parsed_latest.astimezone(
                    ZoneInfo("Asia/Kolkata")
                )
                now_check_ist = datetime.now(
                    ZoneInfo("Asia/Kolkata")
                )

                market_open_now = (
                    now_check_ist.weekday() < 5
                    and now_check_ist.time().hour >= 9
                    and (
                        now_check_ist.time().hour < 15
                        or (
                            now_check_ist.time().hour == 15
                            and now_check_ist.time().minute < 30
                        )
                    )
                    and (
                        now_check_ist.time().hour > 9
                        or now_check_ist.time().minute >= 15
                    )
                )

                age_minutes = max(
                    0.0,
                    (now_check_ist - latest_ist).total_seconds() / 60.0,
                )

                max_age = freshness_limits_minutes[interval]

                if market_open_now:
                    if latest_ist.date() != now_check_ist.date():
                        return {
                            "available": False,
                            "stale": True,
                            "interval": interval,
                            "bar_count": len(closes),
                            "timestamps": timestamps,
                            "latest_timestamp": latest_timestamp,
                            "data_source": "angel_one_intraday",
                            "freshness_minutes": round(age_minutes, 1),
                            "reason": (
                                f"Angel One intraday data is stale: "
                                f"latest candle {latest_timestamp} is from "
                                f"an earlier trading session"
                            ),
                        }

                    if age_minutes > max_age:
                        return {
                            "available": False,
                            "stale": True,
                            "interval": interval,
                            "bar_count": len(closes),
                            "timestamps": timestamps,
                            "latest_timestamp": latest_timestamp,
                            "data_source": "angel_one_intraday",
                            "freshness_minutes": round(age_minutes, 1),
                            "max_freshness_minutes": max_age,
                            "reason": (
                                f"Angel One intraday data is stale: "
                                f"latest candle is {age_minutes:.1f} minutes old "
                                f"(limit {max_age} minutes)"
                            ),
                        }

            except Exception as e:
                logger.warning(
                    f"Could not validate Angel One intraday timestamp for "
                    f"{symbol} {interval}: {e}"
                )

        # Keep the broker response explicit about candle freshness.
        # The safety validation above already rejects stale previous-session
        # data during market hours. These fields expose the same information
        # to API/UI callers without changing the existing availability gate.
        freshness_minutes = None
        session_date = None
        fresh = False

        if latest_timestamp:
            try:
                ts_text = str(latest_timestamp).strip()

                if ts_text.endswith("Z"):
                    parsed_latest = datetime.fromisoformat(
                        ts_text[:-1] + "+00:00"
                    )
                else:
                    parsed_latest = datetime.fromisoformat(ts_text)

                if parsed_latest.tzinfo is None:
                    parsed_latest = parsed_latest.replace(
                        tzinfo=ZoneInfo("Asia/Kolkata")
                    )

                latest_ist = parsed_latest.astimezone(
                    ZoneInfo("Asia/Kolkata")
                )
                now_ist = current_ist()

                freshness_minutes = round(
                    max(
                        0.0,
                        (now_ist - latest_ist).total_seconds() / 60.0,
                    ),
                    1,
                )
                session_date = latest_ist.date().isoformat()

                max_age = freshness_limits_minutes.get(interval)
                market_open_now = (
                    now_ist.weekday() < 5
                    and (
                        now_ist.time().hour > 9
                        or (
                            now_ist.time().hour == 9
                            and now_ist.time().minute >= 15
                        )
                    )
                    and (
                        now_ist.time().hour < 15
                        or (
                            now_ist.time().hour == 15
                            and now_ist.time().minute < 30
                        )
                    )
                )

                fresh = bool(
                    market_open_now
                    and latest_ist.date() == now_ist.date()
                    and max_age is not None
                    and freshness_minutes <= max_age
                )

            except Exception as e:
                logger.debug(
                    f"Could not build Angel One freshness metadata "
                    f"for {symbol} {interval}: {e}"
                )

        return {
            "available": True,
            "interval": interval,
            "opens": opens, "highs": highs, "lows": lows,
            "closes": closes, "volumes": volumes,
            "timestamps": timestamps,
            "bar_count": len(closes),
            "data_source": "angel_one_intraday",
            "latest_timestamp": latest_timestamp,
            "fresh": fresh,
            "freshness_minutes": freshness_minutes,
            "session_date": session_date,
            "market_open": (
                market_open_now
                if "market_open_now" in locals()
                else None
            ),
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
            "ONE_MINUTE": 2, "FIVE_MINUTE": 25, "FIFTEEN_MINUTE": 25,
            "THIRTY_MINUTE": 15, "ONE_HOUR": 20, "ONE_DAY": 90,
        }.get(interval, 5)

        to_date = current_ist()
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

        timestamps = [c.get("timestamp", "") for c in candles]
        latest_timestamp = timestamps[-1] if timestamps else None

        freshness_limits_minutes = {
            "ONE_MINUTE": 5,
            "FIVE_MINUTE": 15,
            "FIFTEEN_MINUTE": 30,
            "THIRTY_MINUTE": 60,
            "ONE_HOUR": 90,
        }

        freshness_minutes = None
        session_date = None
        fresh = False
        market_open_now = False

        if latest_timestamp and interval in freshness_limits_minutes:
            try:
                ts_text = str(latest_timestamp).strip()

                if ts_text.endswith("Z"):
                    parsed_latest = datetime.fromisoformat(
                        ts_text[:-1] + "+00:00"
                    )
                else:
                    parsed_latest = datetime.fromisoformat(ts_text)

                if parsed_latest.tzinfo is None:
                    parsed_latest = parsed_latest.replace(
                        tzinfo=ZoneInfo("Asia/Kolkata")
                    )

                latest_ist = parsed_latest.astimezone(
                    ZoneInfo("Asia/Kolkata")
                )
                now_ist = current_ist()

                freshness_minutes = round(
                    max(
                        0.0,
                        (now_ist - latest_ist).total_seconds() / 60.0,
                    ),
                    1,
                )
                session_date = latest_ist.date().isoformat()

                market_open_now = (
                    now_ist.weekday() < 5
                    and (
                        now_ist.time().hour > 9
                        or (
                            now_ist.time().hour == 9
                            and now_ist.time().minute >= 15
                        )
                    )
                    and (
                        now_ist.time().hour < 15
                        or (
                            now_ist.time().hour == 15
                            and now_ist.time().minute < 30
                        )
                    )
                )

                max_age = freshness_limits_minutes[interval]

                if market_open_now:
                    if latest_ist.date() != now_ist.date():
                        return {
                            "available": False,
                            "stale": True,
                            "interval": interval,
                            "bar_count": len(candles),
                            "timestamps": timestamps,
                            "latest_timestamp": latest_timestamp,
                            "data_source": "zerodha_intraday",
                            "freshness_minutes": freshness_minutes,
                            "reason": (
                                f"Zerodha intraday data is stale: "
                                f"latest candle {latest_timestamp} is from "
                                f"an earlier trading session"
                            ),
                        }

                    if freshness_minutes > max_age:
                        return {
                            "available": False,
                            "stale": True,
                            "interval": interval,
                            "bar_count": len(candles),
                            "timestamps": timestamps,
                            "latest_timestamp": latest_timestamp,
                            "data_source": "zerodha_intraday",
                            "freshness_minutes": freshness_minutes,
                            "max_freshness_minutes": max_age,
                            "reason": (
                                f"Zerodha intraday data is stale: "
                                f"latest candle is {freshness_minutes:.1f} "
                                f"minutes old (limit {max_age} minutes)"
                            ),
                        }

                fresh = bool(
                    market_open_now
                    and latest_ist.date() == now_ist.date()
                    and freshness_minutes <= max_age
                )

            except Exception as e:
                logger.debug(
                    f"Could not build Zerodha freshness metadata "
                    f"for {symbol} {interval}: {e}"
                )

        return {
            "available": True,
            "interval": interval,
            "highs":   [safe_float(c.get("high"))  for c in candles],
            "lows":    [safe_float(c.get("low"))   for c in candles],
            "closes":  [safe_float(c.get("close")) for c in candles],
            "volumes": [safe_int(c.get("volume", 0)) for c in candles],
            "timestamps": timestamps,
            "bar_count": len(candles),
            "data_source": "zerodha_intraday",
            "latest_timestamp": latest_timestamp,
            "fresh": fresh,
            "freshness_minutes": freshness_minutes,
            "session_date": session_date,
            "market_open": market_open_now,
        }

    # BUG FIX (2026-08-22): TTL was 15s but the dashboard auto-refreshes
    # every 30s (see ltAutoInterval in dashboard.html) — every single
    # auto-refresh cycle was guaranteed to miss this cache and fire a fresh
    # Angel One call, adding to the account-wide call volume that was
    # tripping Angel's rate limiter. 25s still keeps the number fresh
    # within roughly one refresh cycle, just no longer *always* refetching.
    async def _fetch_futures_premium_live(self, symbol: str) -> Dict:
        """
        Fetch one live futures premium value from Angel One.

        This helper deliberately bypasses the generic async_cache decorator.
        The caller owns the short-lived stale-while-refresh cache below.
        """
        symbol = str(symbol).upper().strip()

        try:
            from app.services.angel_one import angel_session
        except Exception:
            return {
                "status": "unavailable",
                "premium": 0.0,
                "premium_pct": 0.0,
            }

        if not angel_session.is_configured:
            return {
                "status": "unavailable",
                "premium": 0.0,
                "premium_pct": 0.0,
            }

        try:
            spot = await self.get_spot(symbol)
            spot_price = safe_float(spot.get("price", 0))
        except Exception as e:
            logger.warning(
                f"Spot fetch for futures-premium calc failed for {symbol}: {e}"
            )
            return {
                "status": "unavailable",
                "premium": 0.0,
                "premium_pct": 0.0,
            }

        if spot_price <= 0:
            return {
                "status": "unavailable",
                "premium": 0.0,
                "premium_pct": 0.0,
            }

        try:
            fut = await angel_session.get_futures_ltp(symbol)
        except Exception as e:
            logger.warning(
                f"Futures premium fetch failed for {symbol}: {e}"
            )
            return {
                "status": "unavailable",
                "premium": 0.0,
                "premium_pct": 0.0,
            }

        if not fut:
            return {
                "status": "unavailable",
                "premium": 0.0,
                "premium_pct": 0.0,
            }

        premium = round(fut["ltp"] - spot_price, 2)
        premium_pct = (
            round((premium / spot_price) * 100, 3)
            if spot_price > 0 else 0.0
        )

        return {
            "status": "live",
            "premium": premium,
            "premium_pct": premium_pct,
            "futures_ltp": fut["ltp"],
            "futures_expiry": fut.get("expiry", ""),
        }

    async def _refresh_futures_premium_background(self, symbol: str) -> None:
        """Refresh one futures premium without blocking the HTTP caller."""
        symbol = str(symbol).upper().strip()

        try:
            async with self._futures_premium_lock:
                result = await self._fetch_futures_premium_live(symbol)

                if result.get("status") == "live":
                    self._futures_premium_cache[symbol] = result
                    self._futures_premium_cache_ts[symbol] = time.time()
                    logger.debug(
                        "Futures premium background refresh complete: %s",
                        symbol,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Futures premium background refresh failed for %s: %s",
                symbol,
                e,
            )
        finally:
            self._futures_premium_refresh_tasks.pop(symbol, None)

    def _schedule_futures_premium_refresh(self, symbol: str) -> None:
        """Start at most one background refresh for this symbol."""
        symbol = str(symbol).upper().strip()

        task = self._futures_premium_refresh_tasks.get(symbol)
        if task is not None and not task.done():
            return

        task = asyncio.create_task(
            self._refresh_futures_premium_background(symbol)
        )
        self._futures_premium_refresh_tasks[symbol] = task

    async def get_futures_premium(self, symbol: str) -> Dict:
        """
        Return the latest futures premium without making normal HTTP requests
        wait for every 25-second cache expiry.

        Cache policy:
          <=15s   : return immediately
          15-60s  : return immediately + one background refresh
          >60s    : perform a foreground refresh
        """
        symbol = str(symbol).upper().strip()
        now = time.time()

        cached = self._futures_premium_cache.get(symbol)
        cached_ts = self._futures_premium_cache_ts.get(symbol, 0.0)
        age = now - cached_ts if cached is not None and cached_ts > 0 else float("inf")

        if cached is not None and cached.get("status") == "live":
            if age <= self._futures_premium_refresh_after:
                return cached

            if age <= self._futures_premium_hard_stale_after:
                self._schedule_futures_premium_refresh(symbol)
                return cached

        # No usable cache, or cache is too old: one caller refreshes.
        async with self._futures_premium_lock:
            # Another request may have refreshed while we waited.
            cached = self._futures_premium_cache.get(symbol)
            cached_ts = self._futures_premium_cache_ts.get(symbol, 0.0)
            age = (
                time.time() - cached_ts
                if cached is not None and cached_ts > 0
                else float("inf")
            )

            if (
                cached is not None
                and cached.get("status") == "live"
                and age <= self._futures_premium_hard_stale_after
            ):
                if age > self._futures_premium_refresh_after:
                    self._schedule_futures_premium_refresh(symbol)
                return cached

            result = await self._fetch_futures_premium_live(symbol)

            if result.get("status") == "live":
                self._futures_premium_cache[symbol] = result
                self._futures_premium_cache_ts[symbol] = time.time()

            return result

    @async_cache(ttl=1800)
    async def _get_historical_prices_base(
        self,
        symbol: str,
        days: int = 60,
    ) -> Dict:
        """
        Shared historical OHLC cache.

        Volume proxy is deliberately excluded so foreground and
        background callers share the same historical cache.
        """
        angel_hist = await self._try_angel_historical(
            symbol,
            days,
            include_volume_proxy=False,
        )
        if angel_hist:
            return angel_hist

        zerodha_hist = await self._try_zerodha_historical(symbol, days)
        if zerodha_hist:
            return zerodha_hist

        index_map = {
            "NIFTY": "NIFTY 50",
            "BANKNIFTY": "BANK NIFTY",
            "FINNIFTY": "NIFTY FINANCIAL SERVICES",
        }
        index_name = index_map.get(symbol.upper())
        if not index_name:
            raise ValueError(f"Unsupported symbol: {symbol}")

        to_date = current_ist().date()
        from_date = to_date - timedelta(days=days)

        params = {
            "indexType": index_name,
            "from": from_date.strftime("%d-%m-%Y"),
            "to": to_date.strftime("%d-%m-%Y"),
        }

        raw = await self._get(
            "historicalOR/indicesHistory",
            params=params,
        )

        rows = None

        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict):
            block = raw.get("data")

            if isinstance(block, list):
                rows = block
            elif isinstance(block, dict):
                for key in (
                    "indexCloseOnlineRecords",
                    "indexCloseOnline",
                    "close",
                    "indexData",
                ):
                    candidate = block.get(key)
                    if isinstance(candidate, list) and candidate:
                        rows = candidate
                        break

                if not rows:
                    inner = block.get("data")
                    if isinstance(inner, list) and inner:
                        rows = inner

        if not rows:
            raise MarketDataError(
                "Unexpected NSE historical response shape"
            )

        parsed = []

        for row in rows:
            if not isinstance(row, dict):
                continue

            cv = (
                row.get("EOD_CLOSE_INDEX_VAL")
                or row.get("CLOSE")
                or row.get("close")
            )

            ts = (
                row.get("EOD_TIMESTAMP")
                or row.get("TIMESTAMP")
                or row.get("timestamp")
            )

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
            raise MarketDataError(
                "NSE historical response had no parseable rows"
            )

        parsed.sort(key=lambda r: r[0])

        return {
            "closes": [c for _, c in parsed],
            "volumes": [],
        }

    async def get_historical_prices(
        self,
        symbol: str,
        days: int = 60,
        include_volume_proxy: bool = True,
    ) -> Dict:
        """
        Public historical API.

        Historical OHLC uses one shared cache. Optional futures-volume
        enrichment remains background-only and is not part of the
        shared foreground cache.
        """
        result = await self._get_historical_prices_base(
            symbol,
            days,
        )

        if not include_volume_proxy:
            return result

        volumes = result.get("volumes") or []
        closes = result.get("closes") or []

        if not closes or any(volumes):
            return result

        try:
            from app.services.angel_one import (
                angel_session,
                ANGEL_PRIORITY_BACKGROUND,
                get_angel_call_priority,
            )

            if get_angel_call_priority() != ANGEL_PRIORITY_BACKGROUND:
                return result

            if not angel_session.is_configured:
                return result

            from_date = (
                current_ist() - timedelta(days=days)
            ).strftime("%Y-%m-%d 09:15")

            to_date = current_ist().strftime(
                "%Y-%m-%d %H:%M"
            )

            fut_candles = await angel_session.get_futures_candle_data(
                symbol,
                interval="ONE_DAY",
                from_date=from_date,
                to_date=to_date,
            )

            fut_volumes = [
                safe_int(c.get("volume", 0))
                for c in fut_candles
                if c.get("close")
            ]

            if (
                fut_volumes
                and any(fut_volumes)
                and len(fut_volumes) == len(closes)
            ):
                enriched = dict(result)
                enriched["volumes"] = fut_volumes

                logger.info(
                    f"Using {symbol} futures volume as proxy — "
                    f"{len(fut_volumes)} bars"
                )

                return enriched

        except Exception as e:
            logger.debug(
                f"Futures volume proxy unavailable for {symbol}: {e}"
            )

        return result

    @async_cache(ttl=10)
    async def get_spot(self, symbol: str) -> Dict:
        # Prefer fresh Angel One WebSocket LTP.
        # If the exchange timestamp is stale/missing, the existing
        # Angel REST -> Zerodha -> NSE fallback chain remains intact.
        live_result = await self._try_angel_live_feed_spot(symbol)
        if live_result is not None:
            return live_result

        angel_result = await self._try_angel_spot(symbol)
        if angel_result is not None:
            return angel_result


        # SENSEX is BSE cash. Keep it isolated from the
        # NSE/Zerodha fallback path.
        if symbol.upper() == "SENSEX":
            raise MarketDataError(
                "SENSEX spot unavailable from Angel One BSE; "
                "NSE/Zerodha fallback is disabled"
            )

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
            "timestamp":            current_ist().isoformat(),
        }

    async def _nse_option_chain_v3(self, sym: str, expiry: Optional[str]) -> Dict:
        """
        NSE moved its option-chain API to a two-step v3 contract at some
        point after this project's original single-call integration
        (`option-chain-indices`) was written — that endpoint now 404s
        (confirmed in production: "NSE 404 on option-chain-indices").
        New flow:
          1. GET option-chain-contract-info?symbol=SYM  -> expiry dates
          2. GET option-chain-v3?type=Indices&symbol=SYM&expiry=<one of those>
        Both steps reuse the existing _get() (session/cookie/retry
        handling unchanged). Raises on any failure — the caller falls
        back to the old endpoint, so this can't newly break anything.
        """
        info = await self._get("option-chain-contract-info", params={"symbol": sym})
        expiry_list = (
            (info or {}).get("expiryDates")
            or ((info or {}).get("records") or {}).get("expiryDates")
            or []
        )
        if not expiry_list:
            raise MarketDataError("NSE option-chain-v3: no expiry dates from contract-info")

        if not expiry:
            today = current_ist().date()
            future = [(d, e) for e in expiry_list if (d := _parse_expiry_date(e)) >= today]
            if not future:
                raise MarketDataError("No future expiry found in NSE option-chain-v3 contract-info")
            future.sort()
            expiry = future[0][1]

        raw = await self._get(
            "option-chain-v3", params={"type": "Indices", "symbol": sym, "expiry": expiry}
        )
        # v3's response shape isn't confirmed from this sandbox (no network
        # access to NSE) — handle both a "records"-wrapped shape (like the
        # old endpoint) and a flat top-level shape, whichever it turns out
        # to be, rather than assuming one.
        records = (raw or {}).get("records")
        records = records if isinstance(records, dict) else (raw or {})
        all_strikes = records.get("data", [])
        if not all_strikes:
            raise MarketDataError("NSE option-chain-v3: empty data for expiry")

        chain_rows = [r for r in all_strikes if r.get("expiryDate") == expiry] or all_strikes
        underlying = safe_float(records.get("underlyingValue", (raw or {}).get("underlyingValue", 0)))
        sorted_expiries = sorted(expiry_list, key=_parse_expiry_date)
        return {
            "symbol":           sym,
            "expiry":           expiry,
            "all_expiries":     sorted_expiries,
            "underlying_price": underlying,
            "data":             chain_rows,
            "data_source":      f"nse_curl_cffi_v3/{IMPERSONATE}",
        }

    # BUG FIX (2026-08-22): TTL == the dashboard's own 30s auto-refresh
    # interval (see ltAutoInterval in dashboard.html) means the cache
    # expires right around when the next refresh asks for it — timing
    # drift makes this a near-coin-flip cache hit, not a reliable one.
    # 40s guarantees at least one full refresh cycle is served from cache,
    # cutting this endpoint's Angel One call volume meaningfully without
    # making the option chain noticeably less fresh.
    @async_cache(ttl=40)
    async def _get_option_chain_cached(self, symbol: str, expiry: Optional[str] = None, strikes_each_side: Optional[int] = 10) -> Dict:
        sym = symbol.upper()
        if sym not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
            raise ValueError(f"Unsupported symbol: {symbol}")

        # Reuse the same cached/single-flight spot fetch used by the
        # market overview. This prevents Angel option-chain code from
        # making a second get_ltp() call for the same request.
        spot_price = None
        try:
            spot = await self.get_spot(sym)
            if isinstance(spot, dict) and spot.get("data_source") == "angel_one":
                candidate = safe_float(spot.get("price", 0))
                if candidate > 0:
                    spot_price = candidate
        except Exception as e:
            logger.debug(f"Spot reuse for option chain failed for {sym}: {e}")

        angel_result = await self._try_angel_option_chain(
            sym,
            expiry,
            spot_price=spot_price,
            strikes_each_side=strikes_each_side,
        )
        if angel_result is not None:
            return angel_result

        zerodha_result = await self._try_zerodha_option_chain(sym, expiry, strikes_each_side=strikes_each_side)
        if zerodha_result is not None:
            return zerodha_result

        # SENSEX is a BSE/BFO instrument. Never fall through to
        # the NSE/NFO option-chain path when broker sources fail.
        if sym == "SENSEX":
            raise MarketDataError(
                "SENSEX option chain unavailable from broker sources; "
                "NSE fallback is disabled for BSE/BFO SENSEX"
            )

        try:
            return await self._nse_option_chain_v3(sym, expiry)
        except Exception as e_v3:
            logger.warning(
                f"NSE option-chain-v3 failed for {sym}, trying legacy "
                f"option-chain-indices: {e_v3}"
            )

        raw = await self._get("option-chain-indices", params={"symbol": sym})
        records = (raw or {}).get("records")
        if not records:
            raise MarketDataError("NSE option-chain: missing 'records' block")

        expiry_list = records.get("expiryDates", [])
        if not expiry_list:
            raise MarketDataError("NSE option-chain: no expiry dates")

        if not expiry:
            today = current_ist().date()
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

        # Reuses the shared module-level _parse_expiry_date (see its
        # docstring) — this used to be a near-identical local copy.
        sorted_expiries = sorted(expiry_list, key=_parse_expiry_date)
        return {
            "symbol":           sym,
            "expiry":           expiry,
            "all_expiries":     sorted_expiries,
            "underlying_price": safe_float(records.get("underlyingValue", 0)),
            "data":             chain_rows,
            "data_source":      f"nse_curl_cffi/{IMPERSONATE}",
        }

    # LIVE_OPTION_WS_MERGE_20260923
    def _merge_live_option_ticks(self, chain: Dict) -> Dict:
        """
        Keep REST option-chain caching/contract discovery intact, but overlay
        the freshest Angel WebSocket option values on every public call.

        REST remains authoritative for contract metadata and day-level
        OI change. WebSocket supplies fast-changing market fields.
        """
        if not isinstance(chain, dict):
            return chain

        # Only merge into the Angel-composed chain. Do not mix an Angel
        # WebSocket token with an unrelated fallback-provider chain.
        if chain.get("data_source") != "angel_one":
            return chain

        rows = chain.get("data")
        if not isinstance(rows, list) or not rows:
            return chain

        try:
            from app.services.angel_live_feed import angel_live_feed
            snapshot = angel_live_feed.get_option_snapshot(chain.get("symbol"))
        except Exception as exc:
            logger.debug("Live option WS snapshot unavailable: %s", exc)
            return chain

        if not snapshot:
            return chain

        import time as _time

        chain_expiry = str(chain.get("expiry") or "").strip().upper()
        now = _time.time()
        max_age = 10.0
        merged = 0

        for row in rows:
            if not isinstance(row, dict):
                continue

            for leg_name in ("CE", "PE"):
                leg = row.get(leg_name)
                if not isinstance(leg, dict):
                    continue

                token = str(leg.get("token") or "").strip()
                if not token:
                    continue

                tick = snapshot.get(token)
                if not isinstance(tick, dict):
                    continue

                # Never merge an old tick.
                received_at = tick.get("received_at")
                try:
                    age = now - float(received_at)
                except (TypeError, ValueError):
                    continue

                if age < 0 or age > max_age:
                    continue

                # Exact expiry protection. If either side has no expiry,
                # token identity remains the matching key.
                tick_expiry = str(tick.get("expiry") or "").strip().upper()
                if chain_expiry and tick_expiry and tick_expiry != chain_expiry:
                    continue

                changed = False

                # Fast-changing live market fields.
                if tick.get("ltp") is not None:
                    try:
                        leg["lastPrice"] = float(tick["ltp"])
                        changed = True
                    except (TypeError, ValueError):
                        pass

                if tick.get("volume_trade_for_the_day") is not None:
                    try:
                        leg["totalTradedVolume"] = float(
                            tick["volume_trade_for_the_day"]
                        )
                        changed = True
                    except (TypeError, ValueError):
                        pass

                if tick.get("open_interest") is not None:
                    try:
                        leg["openInterest"] = float(tick["open_interest"])
                        changed = True
                    except (TypeError, ValueError):
                        pass

                if tick.get("bidprice") is not None:
                    try:
                        leg["bidprice"] = float(tick["bidprice"])
                        changed = True
                    except (TypeError, ValueError):
                        pass

                if tick.get("askPrice") is not None:
                    try:
                        leg["askPrice"] = float(tick["askPrice"])
                        changed = True
                    except (TypeError, ValueError):
                        pass

                if tick.get("last_traded_quantity") is not None:
                    leg["lastTradedQuantity"] = tick["last_traded_quantity"]

                if tick.get("total_buy_quantity") is not None:
                    leg["totalBuyQuantity"] = tick["total_buy_quantity"]

                if tick.get("total_sell_quantity") is not None:
                    leg["totalSellQuantity"] = tick["total_sell_quantity"]

                # Explicitly expose freshness/source without replacing the
                # provider identity "angel_one".
                if changed:
                    leg["live_ws"] = True
                    leg["live_ws_received_at"] = received_at
                    leg["live_ws_age_sec"] = round(max(0.0, age), 3)
                    leg["live_ws_source"] = "angel_one_websocket"
                    merged += 1

        if merged:
            chain["live_option_ticks_merged"] = merged
            chain["live_option_data_source"] = "angel_one_websocket"
        else:
            chain["live_option_ticks_merged"] = 0

        return chain

    async def get_option_chain(
        self,
        symbol: str,
        expiry: Optional[str] = None,
        strikes_each_side: Optional[int] = 10,
    ) -> Dict:
        # REST/contract data stays 40-sec cached; only the live overlay is
        # performed outside that cache so every caller sees fresh WS values.
        chain = await self._get_option_chain_cached(symbol, expiry, strikes_each_side=strikes_each_side)
        return self._merge_live_option_ticks(chain)

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
    async def _get_all_indices(self) -> dict:
        """Shared 30s cache for NSE allIndices.

        Multiple consumers (VIX, breadth, sectors) share one
        underlying NSE request instead of separate raw requests.
        """
        return await self._get("allIndices")

    @async_cache(ttl=30)
    async def get_volatility(self) -> float:
        """India VIX — NSE allIndices first, Angel One fallback.

        NSE allIndices is already shared/cached for VIX, breadth and sectors.
        Prefer that source so a normal VIX lookup does not consume an
        additional Angel One throttle slot. Angel One remains a fallback
        when NSE is unavailable or VIX is missing from the response.
        """
        # Primary: shared NSE allIndices cache.
        try:
            data = await self._get_all_indices()
            items = (data or {}).get("data", [])

            for item in items:
                if not isinstance(item, dict):
                    continue

                sym = str(item.get("indexSymbol", "")).upper()
                if "VIX" in sym:
                    val = safe_float(
                        item.get("last", item.get("lastPrice", 0))
                    )
                    if val > 0:
                        logger.info(
                            "India VIX from allIndices (%s): %s",
                            sym,
                            val,
                        )
                        return val

            logger.warning(
                "VIX not found in NSE allIndices; using Angel One fallback"
            )

        except Exception as e:
            logger.warning(
                "NSE allIndices VIX fetch failed; "
                "using Angel One fallback: %s",
                e,
            )

        # Fallback: Angel One.
        try:
            from app.services.angel_one import angel_session

            if angel_session is not None and angel_session.is_configured:
                vix = await angel_session.get_india_vix()
                if vix and vix > 0:
                    return vix

        except Exception as e:
            logger.warning("India VIX via Angel One fallback failed: %s", e)

        return 0.0

    @async_cache(ttl=30)
    async def get_market_breadth(self) -> Dict:
        """NIFTY 50 advance/decline.

        FIX: equity-stockIndices NSE 404 (endpoint deprecated) — removed.
        Now directly uses allIndices which is already called for VIX/sectors.
        One endpoint, one call, cached 30s — no more repeated 404 log lines.

        FIX (2026-08-19): the old code only looked for a nested
        {"advance": {"advances": .., "declines": .., "unchanged": ..}}
        object under the NIFTY 50 row. NSE's allIndices payload does not
        reliably nest breadth that way for the NIFTY 50 row itself — the
        keys are sometimes flat on the item ("advances"/"declines"
        directly, not inside "advance"), sometimes absent entirely for
        that row, and sometimes present only as strings. This version
        tries the nested shape first, then a flat-keys fallback, and
        logs the row's actual keys ONCE (first failure) so the real
        payload shape is visible in the logs instead of a bare warning.
        """
        try:
            data = await self._get_all_indices()
            items = (data or {}).get("data", [])

            nifty50_item = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                sym = item.get("indexSymbol", "")
                # "NIFTY 50" match, exclude "NIFTY BANK", "NIFTY 500" etc.
                if sym.strip().upper() == "NIFTY 50":
                    nifty50_item = item
                    break

            if nifty50_item is not None:
                # Shape 1: nested {"advance": {"advances":.., "declines":.., "unchanged":..}}
                adv = nifty50_item.get("advance")
                if isinstance(adv, dict) and (adv.get("advances") or adv.get("declines")):
                    return {
                        "advances":  safe_int(adv.get("advances",  0)),
                        "declines":  safe_int(adv.get("declines",  0)),
                        "unchanged": safe_int(adv.get("unchanged", 0)),
                        "source":    "nse_allIndices",
                    }

                # Shape 2: flat keys directly on the item
                flat_adv = safe_int(nifty50_item.get("advances", 0))
                flat_dec = safe_int(nifty50_item.get("declines", 0))
                flat_unc = safe_int(nifty50_item.get("unchanged", 0))
                if flat_adv or flat_dec:
                    return {
                        "advances":  flat_adv,
                        "declines":  flat_dec,
                        "unchanged": flat_unc,
                        "source":    "nse_allIndices",
                    }

                # Neither shape had usable data — log the actual keys once
                # so the payload shape can be diagnosed from logs.
                logger.warning(
                    "Market breadth: NIFTY 50 row has no usable advance/decline "
                    f"data — available keys: {sorted(nifty50_item.keys())}"
                )
            else:
                logger.warning(
                    "Market breadth: no 'NIFTY 50' row found in allIndices — "
                    f"symbols present: {sorted({i.get('indexSymbol','') for i in items if isinstance(i, dict)})}"
                )
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
            data = await self._get_all_indices()
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
