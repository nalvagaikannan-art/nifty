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
        _row("GOLDPETAL", "GOLDPETAL30NOV26FUT", "578630", "MCX", "FUTCOM", "30NOV2026"),
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


@pytest.mark.asyncio
async def test_mcx_quote_is_cached_for_repeated_widget_polls(monkeypatch):
    session = AngelOneSession()
    session._instruments = [
        _row("GOLDPETAL", "GOLDPETAL31DEC26FUT", "578631", "MCX", "FUTCOM", "31DEC2026"),
    ]

    async def noop():
        return None

    async def throttle_noop(**kwargs):
        return None

    calls = []
    async def sdk_call(*args):
        calls.append(args)
        return {"data": {"ltp": 12345.0}}

    monkeypatch.setattr(session, "ensure_session", noop)
    monkeypatch.setattr(session, "_ensure_instruments", noop)
    monkeypatch.setattr(session, "_throttle", throttle_noop)
    monkeypatch.setattr(session, "_run_sdk_call", sdk_call)

    first = await session.get_mcx_futures_ltp("GOLDPETAL")
    second = await session.get_mcx_futures_ltp("GOLDPETAL")

    assert first["ltp"] == second["ltp"] == 12345.0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_mcx_candles_use_bounded_history_and_cache(monkeypatch):
    session = AngelOneSession()
    session._instruments = [
        _row("GOLDPETAL", "GOLDPETAL31DEC26FUT", "578631", "MCX", "FUTCOM", "31DEC2026"),
    ]

    async def noop():
        return None

    calls = []
    async def fetch(exchange, token, interval, from_date, to_date, **kwargs):
        calls.append((exchange, token, interval, from_date, to_date, kwargs))
        return [{"timestamp": "2026-11-27T12:00:00+05:30", "open": 1, "high": 2,
                 "low": 1, "close": 2, "volume": 1}]

    monkeypatch.setattr(session, "ensure_session", noop)
    monkeypatch.setattr(session, "_ensure_instruments", noop)
    monkeypatch.setattr(session, "_fetch_candles", fetch)

    first = await session.get_mcx_futures_candle_data("GOLDPETAL")
    second = await session.get_mcx_futures_candle_data("GOLDPETAL")

    assert first == second
    assert len(calls) == 1
    assert calls[0][0:3] == ("MCX", "578631", "FIVE_MINUTE")
    assert calls[0][5]["fail_fast_rate_limit"] is True
    assert calls[0][3] != "" and calls[0][4] != ""


@pytest.mark.asyncio
async def test_mcx_contract_list_is_pinned_to_approved_pilot_expiry(monkeypatch):
    session = AngelOneSession()
    session._instruments = [
        _row("GOLDPETAL", "GOLDPETAL30NOV26FUT", "578630", "MCX", "FUTCOM", "30NOV2026"),
        _row("GOLDPETAL", "GOLDPETAL31DEC26FUT", "578631", "MCX", "FUTCOM", "31DEC2026"),
    ]

    async def noop():
        return None

    monkeypatch.setattr(session, "ensure_session", noop)
    monkeypatch.setattr(session, "_ensure_instruments", noop)

    contracts = await session.get_mcx_futures_contracts("GOLDPETAL")

    assert len(contracts) == 1
    assert contracts[0]["token"] == "578631"
    assert contracts[0]["tradingsymbol"] == "GOLDPETAL31DEC26FUT"
