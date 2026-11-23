import asyncio

import pytest
from fastapi import HTTPException


class FakeFetcher:
    def __init__(self, supported):
        self.supported = supported
        self.spot_calls = []
        self.chain_calls = []

    async def is_supported_market_symbol(self, symbol):
        return self.supported

    async def get_spot(self, symbol):
        self.spot_calls.append(symbol)
        return {"symbol": symbol, "price": 100.0}

    async def get_option_chain(self, symbol, **kwargs):
        self.chain_calls.append(symbol)
        return {"symbol": symbol, "data": []}


def test_market_spot_rejects_symbol_outside_current_fno_universe():
    from app.api.routes.market import get_spot

    fetcher = FakeFetcher(False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_spot("RELIANCE", fetcher))
    assert exc.value.status_code == 400
    assert fetcher.spot_calls == []


def test_market_spot_normalizes_and_accepts_verified_fno_symbol():
    from app.api.routes.market import get_spot

    fetcher = FakeFetcher(True)
    result = asyncio.run(get_spot("reliance", fetcher))
    assert result["symbol"] == "RELIANCE"
    assert fetcher.spot_calls == ["RELIANCE"]


def test_option_pcr_rejects_symbol_outside_current_fno_universe():
    from app.api.routes.options import get_pcr

    fetcher = FakeFetcher(False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_pcr("RELIANCE", fetcher))
    assert exc.value.status_code == 400
    assert fetcher.chain_calls == []


def test_option_chain_rejects_symbol_outside_current_fno_universe():
    from app.api.routes.options import get_option_chain

    fetcher = FakeFetcher(False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_option_chain("RELIANCE", fetcher=fetcher))
    assert exc.value.status_code == 400
    assert fetcher.chain_calls == []
