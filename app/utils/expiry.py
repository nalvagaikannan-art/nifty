"""Canonical option-expiry keys and legacy-format query compatibility."""
from datetime import date, datetime

from sqlalchemy import func, or_


def _parse_expiry_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    raw = str(value or "").strip()
    if not raw:
        return None

    formats = (
        "%d%b%Y", "%d-%b-%Y", "%d %b %Y",
        "%d%B%Y", "%d-%B-%Y", "%d %B %Y",
        "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
        "%Y/%m/%d", "%Y%m%d", "%d%m%Y",
        "%d%b%y", "%d-%b-%y",
    )
    for candidate in (raw, raw.upper(), raw.title()):
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                pass
    return None


def canonical_expiry(value):
    """Return DDMMMYYYY (for example 27OCT2026) when parseable."""
    parsed = _parse_expiry_date(value)
    return parsed.strftime("%d%b%Y").upper() if parsed else str(value or "").strip()


def expiry_storage_variants(value):
    """Return common legacy spellings without changing stored DB rows."""
    raw = str(value or "").strip()
    parsed = _parse_expiry_date(value)
    if parsed is None:
        return (raw,) if raw else ()

    bases = (
        parsed.strftime("%d%b%Y"),
        parsed.strftime("%d-%b-%Y"),
        parsed.strftime("%Y-%m-%d"),
        parsed.strftime("%d-%m-%Y"),
        parsed.strftime("%d/%m/%Y"),
        parsed.strftime("%Y%m%d"),
    )
    values = [raw]
    for item in bases:
        values.extend((item, item.upper(), item.lower(), item.title()))
    values.append(canonical_expiry(value))
    return tuple(dict.fromkeys(v for v in values if v))


def expiry_sql_key(column):
    """Normalize uppercase, hyphenated and spaced month-name expiry formats."""
    return func.upper(
        func.replace(
            func.replace(func.trim(column), "-", ""),
            " ",
            "",
        )
    )


def expiry_matches(column, value):
    """Match canonical keys and common historical expiry spellings."""
    canonical = canonical_expiry(value)
    if not canonical:
        return column == value
    return or_(
        expiry_sql_key(column) == canonical,
        column.in_(expiry_storage_variants(value)),
    )
