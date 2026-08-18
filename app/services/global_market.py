"""
GlobalMarketService — Free public global-market data
======================================================

Spec-ன் "Global Market" section-ஐ implement செய்கிறது:
  Gift Nifty, Dow/Nasdaq/S&P Futures, Nikkei, Hang Seng, Shanghai, FTSE,
  DAX, CAC, Crude Oil, Gold, Silver, Dollar Index, USDINR, US Bond Yield.

Data source: Yahoo Finance's public `/v8/finance/chart/{symbol}` endpoint.
No API key required, no paid subscription — matches spec's "Free Public
APIs" requirement. This is a *quote* endpoint (JSON), not an HTML page, so
it doesn't violate the "don't scrape HTML" principle applied elsewhere in
this codebase (see data_fetcher.get_fii_dii docstring for the same rule).

Dev rule compliance:
  - Never Mock Data: every field is either a real fetched number or
    explicitly `None` with source="unavailable" for that symbol.
  - If any symbol fails, only THAT symbol is marked unavailable — one
    failed ticker must not blank out the whole global-market panel
    (same "partial failure isolation" pattern used elsewhere, e.g.
    market_analyzer._safe_option_chain).
"""

import asyncio
import logging
import re
from typing import Dict, Optional

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession

logger = logging.getLogger(__name__)

# symbol -> (Yahoo ticker, display name, category)
# Gift Nifty note (updated 2026-08-17): Yahoo does not publish an official
# free real-time feed for NSE IX's Gift Nifty contract under a stable public
# ticker, and Angel One's SmartAPI only covers NSE/NFO/BSE/BFO/MCX/CDS — not
# NSE IX, where Gift Nifty actually trades. investing.com's quote page for
# it (in.investing.com/indices/gift-nifty-50-c1-futures) DOES server-render
# the current price and previous close as plain text (confirmed by manual
# fetch), so that's used as a best-effort fallback source — see
# _fetch_gift_nifty() below. This is a numeric-only scrape (just price and
# prev_close, nothing textual/analytical from the page) using the same
# curl_cffi browser-impersonation technique already proven against NSE in
# data_fetcher.py. NOTE: investing.com's ToS prohibits programmatic
# reproduction of their data without permission — fine for personal/internal
# use, but worth knowing if this is ever exposed publicly or commercially.
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

_GIFT_NIFTY_URL       = "https://in.investing.com/indices/gift-nifty-50-c1-futures"
_GIFT_PRICE_RE        = re.compile(r"current Gift Nifty 50 Futures price is ([\d,]+\.?\d*)", re.IGNORECASE)
_GIFT_PREV_CLOSE_RE   = re.compile(r"Prev\.\s*Close\s*\n\s*([\d,]+\.?\d*)")
_GIFT_IMPERSONATE     = "chrome131"  # same technique as data_fetcher.py's NSE session
_GIFT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
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


class GlobalMarketService:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

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

    async def _fetch_gift_nifty(self) -> Optional[Dict]:
        """Best-effort GIFT Nifty fetch via investing.com's server-rendered
        quote page — see the module-level comment above _TICKERS for why
        this exists and its caveats. Extracts ONLY the current price and
        previous close (two numbers) via anchored regex; never touches any
        of the page's text/analysis/news content. Returns None (→
        'unavailable') on any HTTP error, missing match, or parse failure —
        a page-layout change on investing.com's side degrades gracefully
        instead of crashing the global-market panel."""
        try:
            async with CurlAsyncSession(
                impersonate=_GIFT_IMPERSONATE,
                headers=_GIFT_HEADERS,
                timeout=10,
                verify=True,
                allow_redirects=True,
                max_redirects=5,
            ) as session:
                resp = await session.get(_GIFT_NIFTY_URL)
            if resp.status_code != 200:
                # DIAGNOSTIC (temporary, 2026-08-18): was logger.debug — invisible
                # at Render's default INFO level, so "Data Unavailable" gave no
                # clue whether this was a bot-block, redirect, or regex mismatch.
                # Bumped to WARNING with the actual status + a body snippet so
                # the real cause shows up in logs. Revert to debug() once the
                # cause is confirmed and fixed.
                logger.warning(f"GIFT Nifty fetch got HTTP {resp.status_code} (len={len(resp.text or '')})")
                return None
            html = resp.text
            price_m = _GIFT_PRICE_RE.search(html)
            prev_m  = _GIFT_PREV_CLOSE_RE.search(html)
            if not price_m or not prev_m:
                snippet = re.sub(r"\s+", " ", (html or "")[:300]).strip()
                logger.warning(
                    f"GIFT Nifty page fetched (200, len={len(html)}) but price/prev-close "
                    f"pattern not found — page start: {snippet!r}"
                )
                return None
            price = float(price_m.group(1).replace(",", ""))
            prev  = float(prev_m.group(1).replace(",", ""))
            if prev == 0:
                return None
            change_pct = ((price - prev) / prev) * 100
            return {
                "price":          round(price, 2),
                "prev_close":     round(prev, 2),
                "change_percent": round(change_pct, 2),
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
            "gift_nifty_status":     "investing_com" if gift_result else "unavailable_fetch_failed",
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


# Module-level singleton — same pattern as app.services.angel_one.angel_session
# (cheap to construct, but the underlying httpx.AsyncClient benefits from
# connection reuse across requests, same reasoning as DataFetcher's NSE
# session in app/api/deps.py).
global_market_service = GlobalMarketService()
