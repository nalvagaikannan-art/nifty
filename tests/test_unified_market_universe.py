import asyncio

from app.api.routes.market import get_unified_market_universe


class FakeFetcher:
    async def get_market_symbol_universe(self):
        return {"indices": ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"],
                "stocks": ["RELIANCE", "INFY"], "stocks_available": True}


class FakeAngel:
    async def get_mcx_commodity_catalog(self):
        return {"available": True, "source": "angel_one_instrument_master", "commodities": [
            {"commodity": "GOLD", "futures_contracts": 2, "options_contracts": 4},
            {"commodity": "SILVER", "futures_contracts": 1, "options_contracts": 0},
        ]}


def test_unified_market_universe_keeps_nse_and_mcx_separate():
    result = asyncio.run(get_unified_market_universe(FakeFetcher(), FakeAngel()))
    assert result["nse"]["indices"] == ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
    assert result["nse"]["stocks"] == ["RELIANCE", "INFY"]
    assert [x["commodity"] for x in result["mcx"]["futures"]] == ["GOLD", "SILVER"]
    assert [x["commodity"] for x in result["mcx"]["options_catalog"]] == ["GOLD"]
    assert result["mcx"]["options_live_supported"] is False


class BrokenFetcher:
    async def get_market_symbol_universe(self):
        raise RuntimeError("NSE fixture failure")


class WorkingAngel:
    async def get_mcx_commodity_catalog(self):
        return {"available": True, "commodities": [
            {"commodity": "GOLD", "futures_contracts": 1, "options_contracts": 0}
        ]}


def test_unified_market_universe_isolates_nse_failure_from_mcx():
    result = asyncio.run(get_unified_market_universe(BrokenFetcher(), WorkingAngel()))
    assert result["nse"]["available"] is False
    assert "NSE universe unavailable" in result["nse"]["reason"]
    assert result["mcx"]["available"] is True
    assert result["mcx"]["futures"][0]["commodity"] == "GOLD"


class BrokenAngel:
    async def get_mcx_commodity_catalog(self):
        raise RuntimeError("MCX fixture failure")


def test_unified_market_universe_isolates_mcx_failure_from_nse():
    result = asyncio.run(get_unified_market_universe(FakeFetcher(), BrokenAngel()))
    assert result["nse"]["available"] is True
    assert result["mcx"]["available"] is False
    assert "MCX catalogue unavailable" in result["mcx"]["reason"]