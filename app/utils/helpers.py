import re
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# NSE trading holidays (equity + equity-derivatives segments), keyed by year.
# Weekday holidays only — dates that already fall on Sat/Sun are omitted since
# is_market_hours_ist() already returns False for weekends regardless.
#
# Source: NSE's official published calendar (verified against
# https://www.angelone.in/nse-holidays for 2026 as of Aug 2026).
#
# IMPORTANT: NSE typically publishes each year's holiday circular around
# November/December of the *preceding* year. As of Aug 2026 (this codebase's
# last update), NSE has NOT yet published an official 2027 calendar — third
# party "Indian holidays 2027" lists exist but are generic national-holiday
# lists, not NSE's specific trading-holiday circular (they don't match:
# e.g. general lists include Makar Sankranti / Hindi Diwas, which are not
# NSE trading holidays, while omitting NSE-specific ones like Ram Navami).
# Fabricating those dates here would be worse than leaving the year absent —
# see the fallback behavior in is_market_hours_ist() below.
#
# ACTION REQUIRED EACH DECEMBER: once NSE publishes the next year's circular
# (check https://www.nseindia.com/resources/exchange-communication-holidays),
# add a new `NSE_HOLIDAYS[<year>] = {...}` entry below.
NSE_HOLIDAYS = {
    2026: {
        date(2026, 1, 26),   # Republic Day
        date(2026, 3, 3),    # Holi
        date(2026, 3, 26),   # Shri Ram Navami
        date(2026, 3, 31),   # Shri Mahavir Jayanti
        date(2026, 4, 3),    # Good Friday
        date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
        date(2026, 5, 1),    # Maharashtra Day
        date(2026, 5, 28),   # Bakri Id
        date(2026, 6, 26),   # Muharram
        date(2026, 9, 14),   # Ganesh Chaturthi
        date(2026, 10, 2),   # Mahatma Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 10),  # Diwali–Balipratipada
        date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
        date(2026, 12, 25),  # Christmas
        # Note: Oct 21, 2026 (Wed) is a special Muhurat Trading session on
        # Diwali Laxmi Pujan — NOT a full holiday, deliberately excluded.
    },
    # 2027: not yet published by NSE as of this codebase's last update (Aug
    # 2026) — add here once available. Until then, years with no entry fall
    # back to a weekday+trading-hours-only check (see below), which will
    # incorrectly treat holidays in that year as open days. This is
    # preferable to guessing wrong dates.
}

_warned_missing_years = set()


def is_market_hours_ist(now: datetime = None) -> bool:
    """Fallback estimate of whether NSE cash/derivatives market is open, used
    only when NSE's own `marketStatus` field isn't present in a response.
    NSE regular trading hours: Mon-Fri, 09:15-15:30 IST, excluding published
    exchange holidays for years present in NSE_HOLIDAYS. This intentionally
    does NOT know about ad-hoc circulars/special sessions (e.g. Muhurat
    trading) — treat this as a rough estimate only, prefer
    `market_status_source == "nse"` when available."""
    now = now.astimezone(IST) if now else datetime.now(IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    year_holidays = NSE_HOLIDAYS.get(now.year)
    if year_holidays is None and now.year not in _warned_missing_years:
        _warned_missing_years.add(now.year)
        logger.warning(
            f"No NSE holiday calendar loaded for {now.year} — the market-closed "
            "fallback check will not detect holidays this year (weekends/hours "
            "only). Update NSE_HOLIDAYS in app/utils/helpers.py once NSE "
            "publishes its circular for this year."
        )
    if year_holidays and now.date() in year_holidays:
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def clean_nse_response(data):
    """Clean NSE API response from extra callback wrapper if any."""
    if isinstance(data, str):
        data = re.sub(r'^[^(]*\(', '', data)
        data = re.sub(r'\)$', '', data)
    return data


def expiry_filter(expiry_str: str) -> dict:
    """
    Expiry-day risk info, shared by both /api/strategy (which had this
    already) and /api/analysis (which didn't — so the Analysis page never
    warned about expiry-day gamma risk even though Strategy did). Single
    source of truth now, instead of two copies drifting apart.
    """
    try:
        for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
            try:
                exp_date = datetime.strptime(expiry_str.strip().title(), fmt).date()
                today    = datetime.now().date()
                days_left = (exp_date - today).days
                if days_left == 0:
                    return {"is_expiry": True, "days_left": 0,
                            "warning": "⚠️ EXPIRY DAY — HIGH GAMMA RISK",
                            "note": "Expiry day-ல் fast theta decay + sudden reversals possible"}
                elif days_left == 1:
                    return {"is_expiry": False, "days_left": 1,
                            "warning": "⚠️ EXPIRY TOMORROW — Elevated gamma",
                            "note": ""}
                return {"is_expiry": False, "days_left": days_left, "warning": "", "note": ""}
            except ValueError:
                continue
    except Exception:
        pass
    return {"is_expiry": False, "days_left": 7, "warning": "", "note": ""}


def days_to_expiry(expiry_str: str) -> Optional[int]:
    """Same date-parsing as expiry_filter(), factored out for callers (Greeks
    calc, strategy engine) that just need the raw day count and shouldn't
    have to re-implement the multi-format parsing. Returns None (not a
    fabricated guess) when the string can't be parsed at all."""
    if not expiry_str:
        return None
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            exp_date = datetime.strptime(expiry_str.strip().title(), fmt).date()
            return (exp_date - datetime.now().date()).days
        except ValueError:
            continue
    return None


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0

def safe_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0
