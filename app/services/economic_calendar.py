"""
EconomicCalendarService — Free public economic calendar
=========================================================

Spec-ன் "Economic Calendar" section-ஐ implement செய்கிறது:
  Fed, RBI, CPI, WPI, GDP, Employment, Interest Rate, Budget, Election,
  Holiday, Expiry (Monthly + Weekly).

Two deterministic/real sources, no fabricated dates:

  1. NSE Expiry + NSE Holidays — computed directly from
     app.utils.helpers.NSE_HOLIDAYS (already the source of truth used
     elsewhere in this codebase for market-hours checks). Weekly expiry =
     next Thursday (or the prior trading day if Thursday is a holiday);
     Monthly expiry = last Thursday of the month (same adjustment). This
     is a real, reproducible calculation, not a guess.

  2. Global macro events (Fed, CPI, GDP, Employment, etc.) — Forex
     Factory's free, public, no-key-required JSON feed. This is a JSON
     API response, not an HTML scrape. RBI/Budget/Election dates are NOT
     fabricated here: if the feed doesn't carry an INR-tagged event we
     simply don't invent one — matches the "never generate false data"
     dev rule.

If the live feed is unreachable, macro_events comes back as an empty list
with source="unavailable" — the deterministic NSE expiry/holiday data
still returns normally since it needs no network call.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import httpx

from app.utils.helpers import NSE_HOLIDAYS

logger = logging.getLogger(__name__)

_FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# Currencies whose events matter for NIFTY/BankNifty per the spec (Fed = USD,
# RBI-adjacent global cues also show under USD on this feed; INR-specific
# entries appear directly as INR when present).
_RELEVANT_CURRENCIES = {"USD", "INR"}
_RELEVANT_KEYWORDS = (
    "fed", "fomc", "interest rate", "cpi", "inflation", "wpi", "gdp",
    "employment", "unemployment", "non-farm", "nonfarm", "payroll",
    "rbi", "rate decision", "budget",
)


def _is_holiday(d: date) -> bool:
    return d in NSE_HOLIDAYS.get(d.year, set())


def _prior_trading_day(d: date) -> date:
    """Steps backward over weekends/holidays to the nearest trading day."""
    while d.weekday() >= 5 or _is_holiday(d):
        d -= timedelta(days=1)
    return d


def _next_weekly_expiry(today: date) -> date:
    """Next Thursday from `today` (inclusive), holiday-adjusted backward."""
    days_ahead = (3 - today.weekday()) % 7  # Thursday = weekday 3
    candidate = today + timedelta(days=days_ahead)
    return _prior_trading_day(candidate)


def _monthly_expiry(year: int, month: int) -> date:
    """Last Thursday of the given month, holiday-adjusted backward."""
    if month == 12:
        first_of_next = date(year + 1, 1, 1)
    else:
        first_of_next = date(year, month + 1, 1)
    last_day = first_of_next - timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7  # back up to Thursday
    candidate = last_day - timedelta(days=offset)
    return _prior_trading_day(candidate)


class EconomicCalendarService:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers=_HEADERS, timeout=8.0)
        return self._client

    def get_expiry_calendar(self, today: Optional[date] = None) -> Dict:
        """Deterministic weekly + monthly expiry, no network call."""
        today = today or datetime.now().date()
        weekly = _next_weekly_expiry(today)
        this_month_expiry = _monthly_expiry(today.year, today.month)
        if this_month_expiry < today:
            # This month's monthly expiry already passed — show next month's.
            nm_year, nm_month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
            monthly = _monthly_expiry(nm_year, nm_month)
        else:
            monthly = this_month_expiry

        return {
            "weekly_expiry":  weekly.isoformat(),
            "monthly_expiry": monthly.isoformat(),
            "is_expiry_today": today in (weekly, monthly),
            "source": "computed_nse_calendar",
        }

    async def get_macro_events(self) -> Dict:
        """Live Fed/CPI/GDP/etc. events for this week from a free public feed."""
        try:
            client = await self._ensure_client()
            resp = await client.get(_FF_CALENDAR_URL)
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            raw = resp.json()
        except Exception as e:
            logger.warning(f"Economic calendar fetch failed: {e}")
            return {"events": [], "source": "unavailable"}

        events: List[Dict] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            currency = str(item.get("country", item.get("currency", ""))).upper()
            title = str(item.get("title", ""))
            if currency not in _RELEVANT_CURRENCIES and not any(
                kw in title.lower() for kw in _RELEVANT_KEYWORDS
            ):
                continue
            events.append({
                "title":    title,
                "currency": currency,
                "date":     item.get("date"),
                "impact":   item.get("impact"),
                "forecast": item.get("forecast"),
                "previous": item.get("previous"),
            })

        return {"events": events, "source": "forexfactory_public_feed"}

    async def get_calendar(self) -> Dict:
        expiry = self.get_expiry_calendar()
        macro  = await self.get_macro_events()
        return {
            "expiry": expiry,
            "macro_events": macro["events"],
            "macro_source": macro["source"],
        }

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


economic_calendar_service = EconomicCalendarService()
