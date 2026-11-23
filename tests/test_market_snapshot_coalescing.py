import asyncio
from contextvars import ContextVar
from copy import deepcopy

import pytest

from app.services.market_analyzer import MarketAnalyzer


@pytest.mark.asyncio
async def test_same_refresh_key_shares_snapshot_but_returns_independent_copies(monkeypatch):
    analyzer = MarketAnalyzer(fetcher=object())
    base = {
        "symbol": "NIFTY",
        "timestamp": "base",
        "decision": {"signal_lifecycle": "WAIT"},
    }
    refresh_calls = 0

    async def fake_cached(symbol, expiry=None, cache_bust=""):
        return deepcopy(base)

    async def fake_refresh(cached, symbol, expiry=None):
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.03)
        result = deepcopy(cached)
        result["timestamp"] = "shared-live-snapshot"
        result["spot"] = {"price": 25000.0}
        return result

    monkeypatch.setattr(analyzer, "_get_full_market_overview_cached", fake_cached)
    monkeypatch.setattr(analyzer, "_refresh_live_calculations", fake_refresh)
    monkeypatch.setattr(
        "app.services.market_analyzer.is_market_hours_ist",
        lambda _now: True,
    )

    first, second = await asyncio.gather(
        analyzer.get_full_market_overview("NIFTY", cache_bust="same-token"),
        analyzer.get_full_market_overview("NIFTY", cache_bust="same-token"),
    )

    assert refresh_calls == 1
    assert first["timestamp"] == second["timestamp"] == "shared-live-snapshot"
    assert first is not second
    first["decision"]["signal_lifecycle"] = "CHANGED_BY_CALLER"
    assert second["decision"]["signal_lifecycle"] == "WAIT"


@pytest.mark.asyncio
async def test_different_refresh_tokens_do_not_share_final_snapshot(monkeypatch):
    analyzer = MarketAnalyzer(fetcher=object())
    base = {"symbol": "NIFTY", "timestamp": "base", "decision": {}}
    refresh_calls = 0

    async def fake_cached(symbol, expiry=None, cache_bust=""):
        return deepcopy(base)

    async def fake_refresh(cached, symbol, expiry=None):
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.03)
        result = deepcopy(cached)
        result["timestamp"] = f"refresh-{refresh_calls}"
        return result

    monkeypatch.setattr(analyzer, "_get_full_market_overview_cached", fake_cached)
    monkeypatch.setattr(analyzer, "_refresh_live_calculations", fake_refresh)
    monkeypatch.setattr(
        "app.services.market_analyzer.is_market_hours_ist",
        lambda _now: True,
    )

    await asyncio.gather(
        analyzer.get_full_market_overview("NIFTY", cache_bust="token-a"),
        analyzer.get_full_market_overview("NIFTY", cache_bust="token-b"),
    )

    assert refresh_calls == 2

@pytest.mark.asyncio
async def test_all_supported_symbols_keep_separate_snapshots(monkeypatch):
    analyzer = MarketAnalyzer(fetcher=object())
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
    refresh_symbols = []

    async def fake_cached(symbol, expiry=None, cache_bust=""):
        return {"symbol": symbol, "timestamp": "base", "decision": {}}

    async def fake_refresh(cached, symbol, expiry=None):
        refresh_symbols.append(symbol)
        await asyncio.sleep(0.03)
        result = deepcopy(cached)
        result["refreshed_for"] = symbol
        result["timestamp"] = f"snapshot-{symbol}"
        return result

    monkeypatch.setattr(
        analyzer, "_get_full_market_overview_cached", fake_cached
    )
    monkeypatch.setattr(
        analyzer, "_refresh_live_calculations", fake_refresh
    )
    monkeypatch.setattr(
        "app.services.market_analyzer.is_market_hours_ist",
        lambda _now: True,
    )

    results = await asyncio.gather(
        *(
            analyzer.get_full_market_overview(
                symbol, cache_bust="same-token"
            )
            for symbol in symbols
        )
    )

    assert len(refresh_symbols) == len(symbols)
    assert set(refresh_symbols) == set(symbols)

    for symbol, result in zip(symbols, results):
        assert result["symbol"] == symbol
        assert result["refreshed_for"] == symbol
        assert result["timestamp"] == f"snapshot-{symbol}"


@pytest.mark.asyncio
async def test_foreground_and_background_scopes_do_not_share_live_snapshot(
    monkeypatch,
):
    analyzer = MarketAnalyzer(fetcher=object())
    scope_var = ContextVar(
        "test_live_snapshot_scope", default="foreground"
    )
    refresh_scopes = []

    async def fake_cached(symbol, expiry=None, cache_bust=""):
        return {"symbol": symbol, "timestamp": "base", "decision": {}}

    async def fake_refresh(cached, symbol, expiry=None):
        scope = scope_var.get()
        refresh_scopes.append(scope)
        await asyncio.sleep(0.03)
        result = deepcopy(cached)
        result["refresh_scope"] = scope
        return result

    monkeypatch.setattr(
        analyzer, "_get_full_market_overview_cached", fake_cached
    )
    monkeypatch.setattr(
        analyzer, "_refresh_live_calculations", fake_refresh
    )
    monkeypatch.setattr(
        "app.services.market_analyzer.is_market_hours_ist",
        lambda _now: True,
    )
    monkeypatch.setattr(
        "app.services.market_analyzer._singleflight_scope",
        scope_var.get,
    )

    async def invoke(scope):
        token = scope_var.set(scope)
        try:
            return await analyzer.get_full_market_overview(
                "NIFTY", cache_bust="same-token"
            )
        finally:
            scope_var.reset(token)

    foreground, background = await asyncio.gather(
        invoke("foreground"),
        invoke("background"),
    )

    assert len(refresh_scopes) == 2
    assert set(refresh_scopes) == {"foreground", "background"}
    assert foreground["refresh_scope"] == "foreground"
    assert background["refresh_scope"] == "background"

