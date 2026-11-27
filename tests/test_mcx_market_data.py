from datetime import date

import pytest

from app.services.angel_one import AngelOneSession


def _row(name, symbol, token, exchange, instrument_type, expiry, tick="100.000000", lot="1"):
    return {
        "name": name,
        "symbol": symbol,
        "token": token,
        "exch_seg": exchange,
        "instrumenttype": instrument_type,
        "expiry": expiry,
        "tick_size": tick,
        "lotsize": lot,
    }


def test_instrument_filter_keeps_only_mcx_futcom_rows():
    rows = [
        _row("GOLDPETAL", "GOLDPETAL31DEC26FUT", "578631", "MCX", "FUTCOM", "31DEC2026"),
        _row("GOLD", "GOLD31DEC26FUT", "111", "MCX", "FUTCOM", "31DEC2026"),
        _row("NIFTY", "NIFTY26DECFUT", "222", "NFO", "FUTIDX", "31DEC2026"),
        _row("GOLDPETAL", "GOLDPETALCE", "333", "MCX", "OPTFUT", "31DEC2026"),
    ]

    filtered = AngelOneSession._filter_instrument_master_rows(rows)

    assert "578631" in {row["token"] for row in filtered}
    assert "111" in {row["token"] for row in filtered}
    assert "222" in {row["token"] for row in filtered}
    assert "333" not in {row["token"] for row in filtered}


@pytest.mark.asyncio
async def test_mcx_resolver_returns_contract_metadata_without_changing_index_paths(monkeypatch):
    session = AngelOneSession()
    rows = [
        _row("GOLDPETAL", "GOLDPETAL31DEC26FUT", "578631", "MCX", "FUTCOM", "31DEC2026"),
        _row("NIFTY", "NIFTY30DEC26FUT", "999", "NFO", "FUTIDX", "30DEC2026"),
    ]

    async def noop():
        return None

    monkeypatch.setattr(session, "ensure_session", noop)
    monkeypatch.setattr(session, "_ensure_instruments", noop)
    session._instruments = rows

    result = await session._resolve_mcx_futures_token("goldpetal")

    assert result == {
        "exchange": "MCX",
        "token": "578631",
        "name": "GOLDPETAL",
        "tradingsymbol": "GOLDPETAL31DEC26FUT",
        "expiry": "2026-12-31",
        "tick_size": 1.0,
        "lot_size": 1,
        "instrumenttype": "FUTCOM",
    }


@pytest.mark.asyncio
async def test_mcx_resolver_rejects_unknown_or_non_mcx_contracts(monkeypatch):
    session = AngelOneSession()

    async def noop():
        return None

    monkeypatch.setattr(session, "ensure_session", noop)
    monkeypatch.setattr(session, "_ensure_instruments", noop)
    session._instruments = [
        _row("GOLDPETAL", "GOLDPETAL31DEC26FUT", "578631", "NFO", "FUTIDX", "31DEC2026"),
    ]

    assert await session._resolve_mcx_futures_token("GOLDPETAL") is None
    assert await session._resolve_mcx_futures_token("../GOLDPETAL") is None
