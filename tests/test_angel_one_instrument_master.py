"""Unit tests for the filtered Angel One instrument master.

These tests use synthetic instrument rows only; no broker or network calls.
"""
import json
from datetime import date

from app.services.angel_one import AngelOneSession


def _master_rows():
    rows = []
    for name, exchange in (
        ("NIFTY", "NFO"),
        ("BANKNIFTY", "NFO"),
        ("FINNIFTY", "NFO"),
        ("SENSEX", "BFO"),
    ):
        rows.append({
            "name": name,
            "exch_seg": exchange,
            "instrumenttype": "OPTIDX",
            "expiry": "31DEC2099",
            "token": f"IDX-{name}",
        })

    rows.extend([
        {
            "name": "RELIANCE", "exch_seg": "NFO",
            "instrumenttype": "OPTSTK", "expiry": "31DEC2099",
            "token": "OPT-RELIANCE", "symbol": "RELIANCE31DEC20992500CE",
        },
        {
            "name": "RELIANCE", "exch_seg": "NFO",
            "instrumenttype": "FUTSTK", "expiry": "31DEC2099",
            "token": "FUT-RELIANCE", "symbol": "RELIANCE31DEC2099FUT",
        },
        {
            "name": "RELIANCE", "exch_seg": "NSE",
            "instrumenttype": "", "token": "EQ-RELIANCE",
            "symbol": "RELIANCE-EQ",
        },
        {
            "name": "TCS", "exch_seg": "NSE",
            "instrumenttype": "", "token": "EQ-TCS",
            "symbol": "TCS-EQ",
        },
        {
            "name": "ABC", "exch_seg": "NFO",
            "instrumenttype": "OPTSTK", "expiry": "31DEC2099",
            "token": "OPT-ABC", "symbol": "ABC31DEC2099100CE",
        },
        {
            "name": "NIFTY", "exch_seg": "NFO",
            "instrumenttype": "OPTSTK", "expiry": "31DEC2099",
            "token": "BAD-INDEX-STOCK", "symbol": "NIFTY31DEC2099CE",
        },
    ])
    return rows


def test_filter_keeps_all_indices_and_only_fno_stock_contracts_and_cash():
    filtered = AngelOneSession._filter_instrument_master_rows(_master_rows())

    assert {row["name"] for row in filtered if row["instrumenttype"] == "OPTIDX"} == {
        "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"
    }
    assert {
        row["instrumenttype"] for row in filtered
        if row.get("name") == "RELIANCE"
    } == {"OPTSTK", "FUTSTK", ""}
    assert any(
        row.get("symbol") == "RELIANCE-EQ" and row.get("exch_seg") == "NSE"
        for row in filtered
    )
    assert not any(row.get("symbol") == "TCS-EQ" for row in filtered)
    assert not any(row.get("token") == "BAD-INDEX-STOCK" for row in filtered)


def test_versioned_cache_round_trip_keeps_stock_and_sensex_rows(tmp_path):
    rows = AngelOneSession._filter_instrument_master_rows(_master_rows())
    cache_file = tmp_path / "instruments.json"
    today = date(2026, 11, 23)

    AngelOneSession._save_persistent_instrument_cache(
        cache_file, today, rows
    )
    loaded = AngelOneSession._load_persistent_instrument_cache(
        cache_file, today
    )

    assert loaded == rows
    assert any(
        row.get("name") == "SENSEX" and row.get("exch_seg") == "BFO"
        for row in loaded
    )
    assert any(
        row.get("name") == "RELIANCE"
        and row.get("instrumenttype") == "OPTSTK"
        for row in loaded
    )
    assert any(
        row.get("symbol") == "RELIANCE-EQ" for row in loaded
    )


def test_legacy_index_only_cache_is_rejected(tmp_path):
    cache_file = tmp_path / "legacy.json"
    cache_file.write_text(
        json.dumps({
            "cache_day": "2026-11-23",
            "rows": _master_rows()[:4],
        }),
        encoding="utf-8",
    )

    assert AngelOneSession._load_persistent_instrument_cache(
        cache_file, date(2026, 11, 23)
    ) is None
