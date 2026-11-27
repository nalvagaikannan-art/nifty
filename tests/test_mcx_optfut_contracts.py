from datetime import date

import pytest

from app.services.mcx_optfut_contracts import (
    normalize_mcx_optfut_row,
    resolve_mcx_optfut_contracts,
)


def _row(name="GOLD", expiry="26FEB2027", strike="10050000",
         symbol="GOLD26FEB27100500CE", option_type="OPTFUT",
         exchange="MCX", tick="50", lot="1", token="T-1"):
    return {
        "name": name, "expiry": expiry, "strike": strike,
        "symbol": symbol, "instrumenttype": option_type,
        "exch_seg": exchange, "tick_size": tick,
        "lotsize": lot, "token": token,
    }


@pytest.mark.parametrize("name,symbol,strike,lot,tick", [
    ("COPPER", "COPPER19FEB271000CE", "100000", "2500", "1"),
    ("CRUDEOIL", "CRUDEOIL15JAN2710000CE", "1000000", "100", "10"),
    ("CRUDEOILM", "CRUDEOILM15JAN2710000PE", "1000000", "10", "5"),
    ("GOLD", "GOLD26FEB27100500CE", "10050000", "1", "50"),
    ("GOLDM", "GOLDM26FEB27100000PE", "10000000", "100", "50"),
    ("NATGASMINI", "NATGASMINI19FEB27100CE", "10000", "250", "5"),
    ("NATURALGAS", "NATURALGAS19FEB27100PE", "10000", "1250", "5"),
    ("SILVER", "SILVER26FEB27100500CE", "10050000", "30", "50"),
    ("SILVERM", "SILVER26FEB27100500PE", "10050000", "5", "50"),
    ("ZINC", "ZINC19FEB27280CE", "28000", "5", "1"),
])
def test_normalizes_all_ten_observed_mcx_option_commodities(name, symbol, strike, lot, tick):
    expiry = "19FEB2027" if "19FEB27" in symbol else "15JAN2027" if "15JAN27" in symbol else "26FEB2027"
    row = _row(name=name, expiry=expiry, strike=strike, symbol=symbol, lot=lot, tick=tick)
    result = normalize_mcx_optfut_row(row, name, today=date(2026, 11, 27))
    assert result is not None
    assert result["exchange"] == "MCX"
    assert result["instrumenttype"] == "OPTFUT"
    assert result["option_type"] == ("PE" if symbol.endswith("PE") else "CE")
    assert result["strike"] == float(strike) / 100
    assert result["lot_size"] == int(lot)
    assert result["tick_size"] == float(tick) / 100


@pytest.mark.parametrize("mutations", [
    {"exch_seg": "NFO"},
    {"instrumenttype": "FUTCOM"},
    {"name": "SILVER"},
    {"token": ""},
    {"lotsize": "0"},
    {"tick_size": "0"},
    {"expiry": "26FEB2026"},
    {"symbol": "GOLD26FEB27100500PE"},
    {"strike": "10000000"},
    {"symbol": "GOLD_BAD_SYMBOL"},
])
def test_rejects_invalid_or_mismatched_master_rows(mutations):
    row = _row()
    row.update(mutations)
    assert normalize_mcx_optfut_row(row, "GOLD", today=date(2026, 11, 27)) is None


def test_option_type_filter_is_strict():
    row = _row()
    assert normalize_mcx_optfut_row(row, "GOLD", date(2026, 11, 27), "PE") is None
    assert normalize_mcx_optfut_row(row, "GOLD", date(2026, 11, 27), "CE") is not None


def test_resolver_sorts_deduplicates_and_filters_expiry():
    rows = [
        _row(token="GOLD-2", strike="10000000", symbol="GOLD26FEB27100000PE"),
        _row(token="GOLD-1", strike="10000000", symbol="GOLD26FEB27100000CE"),
        _row(token="GOLD-1", strike="10000000", symbol="GOLD26FEB27100000CE"),
        _row(token="GOLD-OLD", expiry="26FEB2026", strike="10000000",
             symbol="GOLD26FEB26100000CE"),
    ]
    result = resolve_mcx_optfut_contracts(rows, "GOLD", today=date(2026, 11, 27))
    assert [item["token"] for item in result] == ["GOLD-1", "GOLD-2"]
    assert resolve_mcx_optfut_contracts(rows, "GOLD", today=date(2026, 11, 27),
                                        option_type="PE")[0]["option_type"] == "PE"


def test_unknown_commodity_is_rejected():
    assert normalize_mcx_optfut_row(_row(), "UNKNOWN", today=date(2026, 11, 27)) is None
