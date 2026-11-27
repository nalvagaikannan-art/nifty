import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.angel_one import AngelOneSession


def _option_row(token, symbol, strike, option_type):
    return {
        "name": "GOLD", "expiry": "26FEB2027", "strike": str(strike * 100),
        "symbol": symbol, "instrumenttype": "OPTFUT", "exch_seg": "MCX",
        "tick_size": "50", "lotsize": "1", "token": token,
    }


def test_mcx_optfut_probe_is_one_full_quote_call_for_one_ce_pe_pair():
    async def run():
        session = AngelOneSession.__new__(AngelOneSession)
        session._instruments = [
            _option_row("101", "GOLD26FEB27100000CE", 100000, "CE"),
            _option_row("102", "GOLD26FEB27100000PE", 100000, "PE"),
            _option_row("103", "GOLD26FEB27100500CE", 100500, "CE"),
            _option_row("104", "GOLD26FEB27100500PE", 100500, "PE"),
        ]
        session._obj = SimpleNamespace(getMarketData=lambda **kwargs: None)
        session.ensure_session = AsyncMock()
        session._ensure_instruments = AsyncMock()
        session._throttle = AsyncMock()
        session._run_sdk_call = AsyncMock(return_value={
            "status": True,
            "data": {"fetched": [
                {"symbolToken": "103", "ltp": 1234.5, "exchFeedTime": "2026-11-27 10:15:00",
                 "secret_like_field": "must-not-be-returned"},
                {"symbolToken": "104", "ltp": 1250.0, "exchFeedTime": "2026-11-27 10:15:01"},
            ], "unfetched": []},
        })

        result = await session.probe_mcx_optfut_quotes("GOLD")
        session._run_sdk_call.assert_awaited_once()
        call = session._run_sdk_call.await_args
        assert call.args[0] == "getMarketData"
        assert call.kwargs["mode"] == "FULL"
        tokens = call.kwargs["exchangeTokens"]["MCX"]
        assert len(tokens) == 2
        assert {row["option_type"] for row in result["quotes"]} == {"CE", "PE"}
        assert all(row["quote_received"] for row in result["quotes"])
        assert all("secret_like_field" not in row["quote"] for row in result["quotes"])
        assert result["request_count"] == 1
        assert result["order_api_called"] is False

    asyncio.run(run())
