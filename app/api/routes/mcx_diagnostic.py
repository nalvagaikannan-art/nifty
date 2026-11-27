"""Temporary, authenticated, read-only MCX OPTFUT cache diagnostics.

This endpoint never logs in to Angel One, downloads/refreshes the master, or
writes files. It reads only the existing persistent filtered cache and uses
an explicit response allow-list that excludes broker tokens and credentials.
Remove this router after the QA investigation is complete.
"""
import json
from collections import Counter, defaultdict

from fastapi import APIRouter, HTTPException

from app.services.angel_one import AngelOneSession
from app.utils.helpers import now_ist

router = APIRouter()


def build_diagnostic(payload: object, today: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid_cache_payload")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("invalid_cache_rows")

    mcx = [
        row for row in rows
        if isinstance(row, dict)
        and str(row.get("exch_seg") or "").upper() == "MCX"
        and str(row.get("instrumenttype") or "").upper() == "OPTFUT"
    ]

    grouped = defaultdict(list)
    type_counts = Counter()
    for row in mcx:
        commodity = str(row.get("name") or "").strip().upper() or "UNKNOWN"
        symbol = str(row.get("symbol") or "").strip().upper()
        option_type = "CE" if symbol.endswith("CE") else "PE" if symbol.endswith("PE") else "OTHER"
        type_counts[option_type] += 1
        grouped[commodity].append((option_type, row))

    commodities = []
    for commodity in sorted(grouped):
        entries = sorted(
            grouped[commodity],
            key=lambda item: (
                str(item[1].get("expiry") or ""),
                str(item[1].get("strike") or ""),
                item[0],
                str(item[1].get("symbol") or ""),
            ),
        )
        commodities.append({
            "name": commodity,
            "rows": len(entries),
            "ce": sum(kind == "CE" for kind, _ in entries),
            "pe": sum(kind == "PE" for kind, _ in entries),
            "other": sum(kind == "OTHER" for kind, _ in entries),
            "samples": [
                {
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
                for kind, row in entries[:8]
            ],
        })

    cache_day = payload.get("cache_day")
    return {
        "mode": "READ_ONLY_EXISTING_CACHE_ONLY",
        "cache_day": cache_day,
        "cache_schema_version": payload.get("schema_version"),
        "cache_freshness": "TODAY" if cache_day == today else "NOT_TODAY",
        "cached_row_count": len(rows),
        "mcx_optfut_row_count": len(mcx),
        "option_type_counts": dict(sorted(type_counts.items())),
        "commodities": commodities,
        "quote_support_tested": False,
        "secrets_or_tokens_included": False,
    }


@router.get("/mcx-optfut-master", include_in_schema=False)
async def mcx_optfut_master_diagnostic():
    cache_file = AngelOneSession._INSTRUMENT_CACHE_FILE
    if not cache_file.is_file():
        raise HTTPException(
            status_code=404,
            detail="Existing instrument cache is missing; no refresh was attempted.",
        )
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        return build_diagnostic(payload, now_ist().date().isoformat())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Existing instrument cache could not be read.",
        ) from exc
