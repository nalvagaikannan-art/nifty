"""Stock instrument resolution tests; synthetic rows only, no broker calls."""
import asyncio

from app.services.angel_one import AngelOneSession


def _session_with_stock_rows():
    session = AngelOneSession()
    session._instruments = [
        {
            "name": "RELIANCE", "exch_seg": "NSE",
            "instrumenttype": "", "symbol": "RELIANCE-EQ",
            "token": "2885",
        },
        {
            "name": "RELIANCE", "exch_seg": "NFO",
            "instrumenttype": "OPTSTK", "expiry": "31DEC2099",
            "symbol": "RELIANCE31DEC20992500CE", "token": "OPT-CE",
            "strike": "250000", "lotsize": "250",
        },
        {
            "name": "RELIANCE", "exch_seg": "NFO",
            "instrumenttype": "OPTSTK", "expiry": "31DEC2099",
            "symbol": "RELIANCE31DEC20992500PE", "token": "OPT-PE",
            "strike": "250000", "lotsize": "250",
        },
        {
            "name": "RELIANCE", "exch_seg": "NFO",
            "instrumenttype": "FUTSTK", "expiry": "31DEC2099",
            "symbol": "RELIANCE31DEC2099FUT", "token": "FUT-RELIANCE",
        },
    ]

    async def _noop():
        return None

    session.ensure_session = _noop
    session._ensure_instruments = _noop
    return session


def test_resolve_nse_cash_eq_token_for_fno_stock():
    session = _session_with_stock_rows()

    resolved = asyncio.run(session._resolve_cash_instrument("reliance"))

    assert resolved == {
        "exchange": "NSE",
        "token": "2885",
        "tradingsymbol": "RELIANCE-EQ",
        "name": "RELIANCE",
    }


def test_resolve_option_tokens_uses_optstk_for_stock():
    session = _session_with_stock_rows()

    resolved = asyncio.run(session.resolve_option_tokens(
        "RELIANCE",
        expiry="31DEC2099",
        strikes_each_side=1,
        spot_price=2500,
    ))

    assert {item["token"] for item in resolved} == {"OPT-CE", "OPT-PE"}
    assert {item["optionType"] for item in resolved} == {"CE", "PE"}
    assert all(item["symbol"] == "RELIANCE" for item in resolved)


def test_resolve_futures_token_uses_futstk_for_stock():
    session = _session_with_stock_rows()

    resolved = asyncio.run(session._resolve_futures_token("RELIANCE"))

    assert resolved["token"] == "FUT-RELIANCE"
    assert resolved["exchange"] == "NFO"
    assert resolved["tradingsymbol"] == "RELIANCE31DEC2099FUT"
