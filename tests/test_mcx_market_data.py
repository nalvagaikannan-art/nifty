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


def test_instrument_filter_keeps_mcx_futures_and_options_rows():
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
    assert "333" in {row["token"] for row in filtered}


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


@pytest.mark.asyncio
@pytest.mark.parametrize("commodity", [
    "CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER", "NICKEL", "ALUMINIUM",
])
async def test_standard_mcx_commodities_resolve_exact_verified_futures(monkeypatch, commodity):
    session = AngelOneSession()

    async def noop():
        return None

    monkeypatch.setattr(session, "ensure_session", noop)
    monkeypatch.setattr(session, "_ensure_instruments", noop)
    session._instruments = [
        _row(commodity, f"{commodity}31DEC26FUT", f"token-{commodity}", "MCX", "FUTCOM", "31DEC2026", tick="100", lot="25"),
    ]

    contract = await session._resolve_mcx_futures_token(commodity)

    assert contract["exchange"] == "MCX"
    assert contract["name"] == commodity
    assert contract["tradingsymbol"] == f"{commodity}31DEC26FUT"
    assert contract["tick_size"] == 1.0
    assert contract["lot_size"] == 25


@pytest.mark.asyncio
async def test_gold_does_not_resolve_goldm_or_goldpetal_as_standard_gold(monkeypatch):
    session = AngelOneSession()

    async def noop():
        return None

    monkeypatch.setattr(session, "ensure_session", noop)
    monkeypatch.setattr(session, "_ensure_instruments", noop)
    session._instruments = [
        _row("GOLDM", "GOLDM31DEC26FUT", "goldm", "MCX", "FUTCOM", "31DEC2026"),
        _row("GOLDPETAL", "GOLDPETAL31DEC26FUT", "goldpetal", "MCX", "FUTCOM", "31DEC2026"),
    ]

    assert await session._resolve_mcx_futures_token("GOLD") is None



@pytest.mark.asyncio
async def test_mcx_catalog_discovers_mini_micro_and_option_coverage(monkeypatch):
    session = AngelOneSession()

    async def noop():
        return None

    monkeypatch.setattr(session, "ensure_session", noop)
    monkeypatch.setattr(session, "_ensure_instruments", noop)
    future_expiry = "31DEC2026"
    session._instruments = [
        _row("GOLD", "GOLD31DEC26FUT", "g1", "MCX", "FUTCOM", future_expiry),
        _row("GOLDM", "GOLDM31DEC26FUT", "gm1", "MCX", "FUTCOM", future_expiry),
        _row("SILVERMIC", "SILVERMIC31DEC26FUT", "sm1", "MCX", "FUTCOM", future_expiry),
        _row("COPPER", "COPPER31DEC26FUT", "c1", "MCX", "FUTCOM", future_expiry),
        _row("COPPER", "COPPER31DEC26CE", "cce", "MCX", "OPTFUT", future_expiry),
        _row("COPPER", "COPPER31DEC26PE", "cpe", "MCX", "OPTFUT", future_expiry),
        _row("NIFTY", "NIFTY31DEC26FUT", "n1", "NFO", "FUTIDX", future_expiry),
    ]

    catalog = await session.get_mcx_commodity_catalog()
    by_name = {item["commodity"]: item for item in catalog["commodities"]}

    assert {"GOLD", "GOLDM", "SILVERMIC", "COPPER"} <= set(by_name)
    assert by_name["COPPER"]["futures_contracts"] == 1
    assert by_name["COPPER"]["options_available"] is True
    assert by_name["COPPER"]["option_types"] == ["CE", "PE"]
    assert "NIFTY" not in by_name


def test_instrument_filter_retains_mcx_options_and_futures_only():
    rows = [
        _row("GOLDM", "GOLDM31DEC26FUT", "f1", "MCX", "FUTCOM", "31DEC2026"),
        _row("GOLDM", "GOLDM31DEC26CE", "c1", "MCX", "OPTFUT", "31DEC2026"),
        _row("GOLDM", "GOLDM31DEC26PE", "p1", "MCX", "OPTFUT", "31DEC2026"),
        _row("NIFTY", "NIFTY26DECFUT", "n1", "NFO", "FUTIDX", "31DEC2026"),
        _row("GOLDM", "GOLDM-EQ", "e1", "NSE", "EQ", "", lot="1"),
    ]

    filtered = AngelOneSession._filter_instrument_master_rows(rows)
    mcx = [r for r in filtered if r.get("exch_seg") == "MCX"]

    assert {r["instrumenttype"] for r in mcx} == {"FUTCOM", "OPTFUT"}
    assert {r["token"] for r in mcx} == {"f1", "c1", "p1"}
