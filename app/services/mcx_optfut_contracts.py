"""QA-stage MCX OPTFUT contract resolver; no broker calls or order execution.

The resolver consumes already-loaded Angel One instrument-master rows only.
It validates the master strike scale against the trading-symbol strike suffix
before exposing a normalized contract. Tick-size normalization follows the
same /100 master convention already used by the MCX FUTCOM resolver.
"""
import re
from datetime import date, datetime


def _expiry_date(value):
    raw = str(value or "").strip()
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def normalize_mcx_optfut_row(row, commodity, today=None, option_type=None):
    """Return one verified MCX option contract, or None for any mismatch."""
    today = today or date.today()
    key = str(commodity or "").strip().upper()
    allowed = {
        "COPPER", "CRUDEOIL", "CRUDEOILM", "GOLD", "GOLDM",
        "NATGASMINI", "NATURALGAS", "SILVER", "SILVERM", "ZINC",
    }
    if key not in allowed or not isinstance(row, dict):
        return None
    if row.get("exch_seg") != "MCX" or str(row.get("instrumenttype") or "").upper() != "OPTFUT":
        return None

    name = str(row.get("name") or "").strip().upper()
    symbol = str(row.get("symbol") or "").strip().upper()
    token = str(row.get("token") or "").strip()
    if name != key or not token:
        return None

    match = re.fullmatch(
        re.escape(key) + r"(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)",
        symbol,
    )
    if not match:
        return None
    symbol_expiry, symbol_strike, suffix_type = match.groups()
    if option_type and str(option_type).strip().upper() != suffix_type:
        return None

    expiry = _expiry_date(row.get("expiry"))
    try:
        symbol_expiry_date = datetime.strptime(symbol_expiry, "%d%b%y").date()
    except ValueError:
        return None
    if expiry is None or expiry != symbol_expiry_date or expiry < today:
        return None

    raw_strike = _positive_number(row.get("strike"))
    encoded_strike = _positive_number(symbol_strike)
    if raw_strike is None or encoded_strike is None:
        return None
    strike = raw_strike / 100.0
    # Instrument-master raw strike must agree with the contract symbol.
    if abs(strike - encoded_strike) > 0.000001:
        return None

    raw_tick = _positive_number(row.get("tick_size"))
    try:
        lot_size_float = float(row.get("lotsize"))
        lot_size = int(lot_size_float)
    except (TypeError, ValueError, OverflowError):
        return None
    if raw_tick is None or lot_size <= 0 or lot_size_float != lot_size:
        return None
    tick_size = raw_tick / 100.0
    if tick_size <= 0:
        return None

    return {
        "exchange": "MCX",
        "instrumenttype": "OPTFUT",
        "commodity": key,
        "token": token,
        "tradingsymbol": symbol,
        "expiry": expiry.isoformat(),
        "strike": strike,
        "option_type": suffix_type,
        "lot_size": lot_size,
        "tick_size": tick_size,
    }


def resolve_mcx_optfut_contracts(instruments, commodity, today=None, option_type=None, expiry=None):
    """Resolve active contracts from an existing instrument list; never fetches data."""
    key = str(commodity or "").strip().upper()
    found = {}
    for row in instruments or []:
        item = normalize_mcx_optfut_row(row, key, today=today, option_type=option_type)
        if item and (not expiry or item["expiry"] == str(expiry)):
            found[item["token"]] = item
    return sorted(
        found.values(),
        key=lambda item: (item["expiry"], item["strike"], item["option_type"], item["tradingsymbol"]),
    )
