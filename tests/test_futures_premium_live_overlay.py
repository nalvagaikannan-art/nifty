import pytest
import pandas as pd
import app.services.market_analyzer as ma
from app.services.market_analyzer import MarketAnalyzer


class FakeFrame:
    empty = False


class FakeFetcher:
    def __init__(self, data):
        self.data = data

    async def _try_angel_live_feed_spot(self, symbol):
        return {"symbol": symbol, "price": 101.0, "prev_close": 99.0}

    async def get_option_chain(self, symbol, expiry=None):
        return {"symbol": symbol, "expiry": expiry or "", "data": [{"strike": 100}]}

    async def get_futures_premium(self, symbol):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


async def exercise(monkeypatch, futures_data):
    analyzer = MarketAnalyzer(fetcher=FakeFetcher(futures_data))
    monkeypatch.setattr(
        analyzer.option,
        "process_option_chain",
        lambda chain: pd.DataFrame([
            {
                "strike": float(strike),
                "ce_oi": 10000,
                "pe_oi": 10000,
                "ce_volume": 5000,
                "pe_volume": 5000,
                "ce_ltp": 2.0,
                "pe_ltp": 2.1,
            }
            for strike in range(95, 105)
        ]),
    )
    monkeypatch.setattr(analyzer.option, "compute_pcr", lambda frame: 1.0)
    monkeypatch.setattr(analyzer.option, "compute_max_pain", lambda frame: 100)
    monkeypatch.setattr(analyzer.option, "compute_oi_change", lambda frame: {})
    monkeypatch.setattr(analyzer.option, "oi_summary", lambda frame, underlying: {})
    monkeypatch.setattr(ma, "classify_market_session_state", lambda data: {})

    seen = []
    def decision(data):
        seen.append((data.get("futures_premium_status"),
                     data.get("futures_premium"),
                     data.get("futures_premium_pct")))
        return {}

    monkeypatch.setattr(ma, "run_decision_engine", decision)
    monkeypatch.setattr(ma, "generate_option_strategy", lambda data, frame: {})
    monkeypatch.setattr(ma, "generate_price_levels", lambda data: {})
    monkeypatch.setattr(ma, "build_tamil_indicators", lambda data, dec: [])

    cached = {
        "symbol": "NIFTY",
        "spot": {"price": 100.0, "prev_close": 99.0},
        "futures_premium": 44.0,
        "futures_premium_pct": 0.4,
        "futures_premium_status": "live",
    }
    result = await analyzer._refresh_live_calculations(cached, "NIFTY", None)
    assert seen, "Decision engine was not reached"
    return result, seen[0]


@pytest.mark.asyncio
async def test_live_futures_value_reaches_decision(monkeypatch):
    result, seen = await exercise(
        monkeypatch, {"status": "live", "premium": -18.5, "premium_pct": -0.07}
    )
    assert result["futures_premium_status"] == "live"
    assert result["futures_premium"] == -18.5
    assert result["futures_premium_pct"] == -0.07
    assert seen == ("live", -18.5, -0.07)


@pytest.mark.asyncio
async def test_stale_futures_value_is_excluded_from_scoring(monkeypatch):
    result, seen = await exercise(
        monkeypatch,
        {"status": "stale", "premium": -18.5, "premium_pct": -0.07,
         "stale_age_sec": 90},
    )
    assert result["futures_premium_status"] == "stale"
    assert result["futures_premium"] == 0.0
    assert result["futures_premium_pct"] == 0.0
    assert seen == ("stale", 0.0, 0.0)


@pytest.mark.asyncio
async def test_overlay_failure_does_not_abort_analysis(monkeypatch):
    result, seen = await exercise(monkeypatch, RuntimeError("test failure"))
    assert result["futures_premium_status"] == "unavailable"
    assert result["futures_premium"] == 0.0
    assert result["futures_premium_pct"] == 0.0
    assert seen == ("unavailable", 0.0, 0.0)
    assert result["spot"]["price"] == 101.0
