"""
GlobalMarketService — Free public global-market data
======================================================

Spec-ன் "Global Market" section-ஐ implement செய்கிறது:
  Gift Nifty, Dow/Nasdaq/S&P Futures, Nikkei, Hang Seng, Shanghai, FTSE,
  DAX, CAC, Crude Oil, Gold, Silver, Dollar Index, USDINR, US Bond Yield.

Data sources:
  - Dow/Nasdaq/S&P/Nikkei/etc: Yahoo Finance's public
    `/v8/finance/chart/{symbol}` endpoint. No API key required.
  - Gift Nifty: NSE's own `/api/marketStatus` endpoint (see
    _fetch_gift_nifty below) — this is NSE's OFFICIAL data for its own
    NSE IX contract, not a third party.

Dev rule compliance:
  - Never Mock Data: every field is either a real fetched number or
    explicitly `None` with source="unavailable" for that symbol.
  - If any symbol fails, only THAT symbol is marked unavailable — one
    failed ticker must not blank out the whole global-market panel
    (same "partial failure isolation" pattern used elsewhere, e.g.
    market_analyzer._safe_option_chain).

GIFT NIFTY — SOURCE HISTORY (read before changing this again):
  1. Yahoo Finance: no public ticker for NSE IX's Gift Nifty contract.
  2. investing.com quote page (numeric-only scrape): worked when tested
     manually, but Render's datacenter IP got an outright HTTP 403 from
     investing.com's edge/WAF (3-byte block response) regardless of
     browser-impersonation headers — this is an IP-reputation block, not
     fixable by changing headers/fingerprint.
  3. CURRENT: NSE's own `https://www.nseindia.com/api/marketStatus`
     endpoint includes a `giftnifty` object (LASTPRICE, DAYCHANGE,
     PERCHANGE, EXPIRYDATE) — NSE's official data for their own
     exchange's contract. This app's NSE session (data_fetcher.py) already
     authenticates against nseindia.com successfully from this same host,
     so the identical cookie-priming approach is replicated here
     (independently — GlobalMarketService doesn't share DataFetcher's
     session/lock, to keep the two services decoupled).
"""

import asyncio
import logging
import time
from typing import Dict, Optional

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession

logger = logging.getLogger(__name__)

# symbol -> (Yahoo ticker, display name, category)
_TICKERS = {
    "dow_futures":     ("YM=F",       "Dow Futures",       "index_futures"),
    "nasdaq_futures":  ("NQ=F",       "Nasdaq Futures",    "index_futures"),
    "sp500_futures":   ("ES=F",       "S&P 500 Futures",   "index_futures"),
    "nikkei":          ("^N225",      "Nikkei 225",        "asia"),
    "hang_seng":       ("^HSI",       "Hang Seng",         "asia"),
    "shanghai":        ("000001.SS",  "Shanghai Composite", "asia"),
    "ftse":            ("^FTSE",      "FTSE 100",          "europe"),
    "dax":             ("^GDAXI",     "DAX",               "europe"),
    "cac":             ("^FCHI",      "CAC 40",            "europe"),
    "crude_oil":       ("CL=F",       "Crude Oil (WTI)",   "commodity"),
    "gold":            ("GC=F",       "Gold",              "commodity"),
    "silver":          ("SI=F",       "Silver",            "commodity"),
    "dollar_index":    ("DX-Y.NYB",   "Dollar Index",      "currency"),
    "usdinr":          ("INR=X",      "USD/INR",           "currency"),
    "us_bond_yield":   ("^TNX",       "US 10Y Bond Yield", "bond"),
}

# Symbols that feed the aggregate "global_change_pct" cue consumed by
# decision_engine._score_global() — the broad overnight-cue basket, not
# every single ticker (e.g. USDINR/bond yield move on different drivers).
_GLOBAL_CUE_KEYS = ("dow_futures", "nasdaq_futures", "sp500_futures", "nikkei", "hang_seng")

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ── NSE marketStatus (Gift Nifty) — same technique as data_fetcher.py ──
_NSE_HOME_URL           = "https://www.nseindia.com/"
_NSE_MARKET_STATUS_URL  = "https://www.nseindia.com/api/marketStatus"
_NSE_IMPERSONATE        = "chrome131"
_NSE_SESSION_TTL        = 240  # seconds — cookies refreshed periodically, same pattern as data_fetcher.SESSION_TTL_SECONDS
_NSE_HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
    "Origin":          "https://www.nseindia.com",
    "DNT":             "1",
    "Connection":      "keep-alive",
}


class GlobalMarketService:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        # Independent NSE session for the marketStatus (Gift Nifty) endpoint
        # — deliberately not shared with data_fetcher.DataFetcher's session
        # to keep these two services decoupled (see module docstring).
        self._nse_session: Optional[CurlAsyncSession] = None
        self._nse_session_ts: float = 0.0
        self._nse_lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(headers=_HEADERS, timeout=8.0)
            return self._client

    async def _fetch_one(self, yahoo_symbol: str) -> Optional[Dict]:
        """Returns {"price", "prev_close", "change_percent"} or None on failure."""
        try:
            client = await self._ensure_client()
            resp = await client.get(_YAHOO_CHART_URL.format(symbol=yahoo_symbol))
            if resp.status_code != 200:
                return None
            data = resp.json()
            result = (((data or {}).get("chart") or {}).get("result") or [None])[0]
            if not result:
                return None
            meta = result.get("meta", {})
            price = meta.get("regularMarketPrice")
            prev  = meta.get("previousClose") or meta.get("chartPreviousClose")
            if price is None or prev is None or prev == 0:
                return None
            change_pct = ((price - prev) / prev) * 100
            return {
                "price":          round(float(price), 2),
                "prev_close":     round(float(prev), 2),
                "change_percent": round(float(change_pct), 2),
            }
        except Exception as e:
            logger.debug(f"Global market fetch failed for {yahoo_symbol}: {e}")
            return None

    async def _ensure_nse_session(self, force: bool = False) -> bool:
        """Primes cookies against nseindia.com's homepage before hitting its
        API — same two-step pattern (homepage first, then API) that
        data_fetcher.py already uses successfully for NSE from this host.
        Returns False (caller should treat as unavailable) if priming fails."""
        now = time.time()
        if not force and self._nse_session and (now - self._nse_session_ts) < _NSE_SESSION_TTL:
            return True

        async with self._nse_lock:
            if not force and self._nse_session and (time.time() - self._nse_session_ts) < _NSE_SESSION_TTL:
                return True

            if self._nse_session:
                try:
                    await self._nse_session.close()
                except Exception:
                    pass

            self._nse_session = CurlAsyncSession(
                impersonate=_NSE_IMPERSONATE,
                headers=_NSE_HEADERS,
                timeout=10,
                verify=True,
                allow_redirects=True,
                max_redirects=10,
            )
            try:
                r1 = await self._nse_session.get(_NSE_HOME_URL)
                if r1.status_code >= 400:
                    logger.warning(f"GIFT Nifty: NSE homepage returned {r1.status_code} while priming session")
                    return False
                self._nse_session_ts = time.time()
                return True
            except Exception as e:
                logger.warning(f"GIFT Nifty: NSE session priming failed: {type(e).__name__}: {e}")
                return False

    async def _fetch_gift_nifty(self) -> Optional[Dict]:
        """GIFT Nifty via NSE's own `/api/marketStatus` endpoint — see the
        module docstring's "SOURCE HISTORY" section for why this replaced
        an earlier investing.com scrape (blocked with HTTP 403 from
        Render's IP). Returns None (→ 'unavailable') on any session,
        HTTP, or parse failure — never crashes the global-market panel."""
        try:
            ok = await self._ensure_nse_session()
            if not ok:
                return None
            resp = await self._nse_session.get(_NSE_MARKET_STATUS_URL)
            if resp.status_code != 200:
                logger.warning(f"GIFT Nifty: marketStatus returned HTTP {resp.status_code}")
                # One retry with a forced fresh session — covers the case
                # where cookies expired server-side before our local TTL did.
                if not await self._ensure_nse_session(force=True):
                    return None
                resp = await self._nse_session.get(_NSE_MARKET_STATUS_URL)
                if resp.status_code != 200:
                    logger.warning(f"GIFT Nifty: marketStatus retry also returned HTTP {resp.status_code}")
                    return None

            data = resp.json()
            gift = (data or {}).get("giftnifty") or {}
            price = gift.get("LASTPRICE")
            change_pct = gift.get("PERCHANGE")
            day_change = gift.get("DAYCHANGE")
            if price is None or change_pct is None:
                logger.warning(f"GIFT Nifty: marketStatus response missing giftnifty fields: {gift!r}")
                return None

            prev_close = None
            if day_change is not None:
                try:
                    prev_close = round(float(price) - float(day_change), 2)
                except Exception:
                    prev_close = None

            return {
                "price":          round(float(price), 2),
                "prev_close":     prev_close,
                "change_percent": round(float(change_pct), 2),
                "expiry":         gift.get("EXPIRYDATE"),
            }
        except Exception as e:
            logger.warning(f"GIFT Nifty fetch failed: {type(e).__name__}: {e}")
            return None

    async def get_snapshot(self) -> Dict:
        """
        Fetches all tracked global instruments concurrently.

        Returns:
          {
            "instruments": {key: {..., "name", "category", "source"} | {"source": "unavailable"}},
            "gift_nifty_change_pct": float | None,
            "global_change_pct": float | None,   # avg of the overnight-cue basket
            "source": "yahoo_finance",
          }
        """
        keys = list(_TICKERS.keys())
        results, gift_result = await asyncio.gather(
            asyncio.gather(*[self._fetch_one(_TICKERS[k][0]) for k in keys]),
            self._fetch_gift_nifty(),
        )

        instruments = {}
        for key, res in zip(keys, results):
            _, name, category = _TICKERS[key]
            if res is None:
                instruments[key] = {"name": name, "category": category, "source": "unavailable"}
            else:
                instruments[key] = {"name": name, "category": category, "source": "yahoo_finance", **res}

        cue_values = [
            instruments[k]["change_percent"]
            for k in _GLOBAL_CUE_KEYS
            if instruments.get(k, {}).get("source") == "yahoo_finance"
        ]
        global_change_pct = round(sum(cue_values) / len(cue_values), 2) if cue_values else None

        return {
            "instruments":           instruments,
            "gift_nifty_change_pct": gift_result["change_percent"] if gift_result else None,
            "gift_nifty_price":      gift_result["price"] if gift_result else None,
            "gift_nifty_expiry":     gift_result["expiry"] if gift_result else None,
            "gift_nifty_status":     "nse_official" if gift_result else "unavailable_fetch_failed",
            "global_change_pct":     global_change_pct,
            "source":                "yahoo_finance",
        }

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        if self._nse_session:
            try:
                await self._nse_session.close()
            except Exception:
                pass
            self._nse_session = None


# Module-level singleton — same pattern as app.services.angel_one.angel_session
# (cheap to construct, but the underlying httpx.AsyncClient benefits from
# connection reuse across requests, same reasoning as DataFetcher's NSE
# session in app/api/deps.py).
global_market_service = GlobalMarketService()
