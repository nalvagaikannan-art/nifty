#!/usr/bin/env python3
"""Read-only diagnostic for the cached Angel One MCX OPTFUT master rows.

This script deliberately does not log in, call SmartAPI, refresh/download the
instrument master, write cache files, or print broker tokens/credentials.
Run from the repository root:
    python scripts/diagnose_mcx_optfut_master.py
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CACHE = Path("data/angel_instrument_master_filtered.json")
IST = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(IST).date().isoformat()
SAMPLE_LIMIT_PER_COMMODITY = 8


def main() -> int:
    print("MCX_OPTFUT_MASTER_DIAGNOSTIC")
    print("MODE=READ_ONLY_CACHE_ONLY")
    print(f"TODAY_IST={TODAY}")
    print(f"CACHE_PATH={CACHE}")

    if not CACHE.is_file():
        print("CACHE_STATUS=MISSING")
        print("RESULT=NO_LOCAL_MASTER_EVIDENCE; no network/login fallback attempted")
        return 2

    try:
        payload = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"CACHE_STATUS=UNREADABLE:{type(exc).__name__}")
        return 2

    if not isinstance(payload, dict):
        print("CACHE_STATUS=INVALID_PAYLOAD")
        return 2

    rows = payload.get("rows")
    print(f"CACHE_DAY={payload.get('cache_day')}")
    print(f"CACHE_SCHEMA_VERSION={payload.get('schema_version')}")
    print(f"CACHE_ROW_COUNT={len(rows) if isinstance(rows, list) else 0}")

    if payload.get("cache_day") != TODAY:
        print("CACHE_FRESHNESS=NOT_TODAY")
    else:
        print("CACHE_FRESHNESS=TODAY")

    if not isinstance(rows, list):
        print("RESULT=INVALID_ROWS")
        return 2

    mcx = [
        row for row in rows
        if isinstance(row, dict)
        and str(row.get("exch_seg") or "").upper() == "MCX"
        and str(row.get("instrumenttype") or "").upper() == "OPTFUT"
    ]
    print(f"MCX_OPTFUT_ROW_COUNT={len(mcx)}")

    by_commodity = defaultdict(list)
    type_counts = Counter()
    for row in mcx:
        commodity = str(row.get("name") or "").strip().upper() or "UNKNOWN"
        symbol = str(row.get("symbol") or "").strip().upper()
        option_type = "CE" if symbol.endswith("CE") else "PE" if symbol.endswith("PE") else "OTHER"
        type_counts[option_type] += 1
        by_commodity[commodity].append((option_type, row))

    print(f"OPTION_TYPE_COUNTS={dict(sorted(type_counts.items()))}")
    print("FIELDS=name,symbol,expiry,strike,lotsize,tick_size,instrumenttype,exch_seg")
    for commodity in sorted(by_commodity):
        entries = sorted(
            by_commodity[commodity],
            key=lambda pair: (
                str(pair[1].get("expiry") or ""),
                str(pair[1].get("strike") or ""),
                pair[0],
                str(pair[1].get("symbol") or ""),
            ),
        )
        types = sorted({kind for kind, _ in entries})
        print(
            f"COMMODITY={commodity} ROWS={len(entries)} "
            f"CE={sum(kind == 'CE' for kind, _ in entries)} "
            f"PE={sum(kind == 'PE' for kind, _ in entries)} "
            f"TYPES={','.join(types)}"
        )
        for kind, row in entries[:SAMPLE_LIMIT_PER_COMMODITY]:
            # Explicit allow-list: never expose token, client IDs, or credentials.
            sample = {
                "name": row.get("name"),
                "symbol": row.get("symbol"),
                "expiry": row.get("expiry"),
                "strike": row.get("strike"),
                "lotsize": row.get("lotsize"),
                "tick_size": row.get("tick_size"),
                "instrumenttype": row.get("instrumenttype"),
                "exch_seg": row.get("exch_seg"),
                "option_type": kind,
            }
            print("SAMPLE=" + json.dumps(sample, ensure_ascii=False, sort_keys=True))

    if not mcx:
        print("RESULT=NO_MCX_OPTFUT_ROWS_IN_LOCAL_CACHE")
    elif payload.get("cache_day") != TODAY:
        print("RESULT=ROWS_FOUND_BUT_CACHE_NOT_CURRENT; do_not_treat_as_live_master")
    else:
        print("RESULT=CURRENT_DAY_CACHED_MASTER_ROWS_FOUND; quote_support_not_tested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
