"""Synthetic tests for the bounded Angel One F&O stock batch scan."""
import asyncio

from app.services.angel_one import AngelOneSession


class _QuoteClient:
    def __init__(self):
        self.calls = []

    def getMarketData(self, mode, exchangeTokens):
        tokens = exchangeTokens["NSE"]
        self.calls.append((mode, list(tokens)))
        return {
            "status": True,
            "data": {
                "fetched": [
                    {
                        "symbolToken": token,
                        "ltp": 112.0 if token == "1001" else 98.0,
                        "open": 105.0,
                        "high": 113.0,
                        "low": 104.0,
                        "close": 100.0,
                        "tradeVolume": 123456,
                    }
                    for token in tokens
                ]
            },
        }


def _session():
    session = AngelOneSession()
    session._instruments = [
        {"name": "RELIANCE", "exch_seg": "NFO", "instrumenttype": "OPTSTK"},
        {"name": "TCS", "exch_seg": "NFO", "instrumenttype": "FUTSTK"},
        {"name": "NIFTY", "exch_seg": "NFO", "instrumenttype": "OPTIDX"},
        {"name": "RELIANCE", "exch_seg": "NSE", "symbol": "RELIANCE-EQ", "token": "1001"},
        {"name": "TCS", "exch_seg": "NSE", "symbol": "TCS-EQ", "token": "1002"},
    ]
    session._obj = _QuoteClient()

    async def _noop(*args, **kwargs):
        return None

    session.ensure_session = _noop
    session._ensure_instruments = _noop
    session._throttle = _noop
    return session


def test_fno_stock_scan_uses_verified_cash_tokens_and_sorts_by_change():
    session = _session()

    result = asyncio.run(session.get_fno_stock_market_scan())

    assert result["available"] is True
    assert result["universe_count"] == 2
    assert result["cash_token_count"] == 2
    assert result["quoted_count"] == 2
    assert [row["symbol"] for row in result["rows"]] == ["RELIANCE", "TCS"]
    assert result["rows"][0]["change_percent"] == 12.0
    assert result["rows"][0]["source"] == "angel_one_marketdata_full"
    assert session._obj.calls == [("FULL", ["1001", "1002"])]


def test_fno_stock_scan_cache_prevents_duplicate_broker_calls():
    session = _session()

    first = asyncio.run(session.get_fno_stock_market_scan())
    second = asyncio.run(session.get_fno_stock_market_scan())

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert len(session._obj.calls) == 1


def test_fno_stock_scan_excludes_ambiguous_cash_tokens():
    session = _session()
    session._instruments.append(
        {"name": "RELIANCE", "exch_seg": "NSE", "symbol": "RELIANCE-EQ", "token": "9999"}
    )

    result = asyncio.run(session.get_fno_stock_market_scan())

    assert result["cash_token_count"] == 1
    assert [row["symbol"] for row in result["rows"]] == ["TCS"]
