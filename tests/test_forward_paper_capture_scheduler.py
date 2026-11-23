import asyncio
from types import SimpleNamespace

import pytest

from app.services import angel_one, history_collector


def test_capture_once_calls_shared_strategy_route_at_background_priority(monkeypatch):
    from app.api.routes import strategy

    calls = []
    analyzer = object()
    ai = object()

    async def no_grace():
        return None

    async def fake_recommendation(**kwargs):
        calls.append({
            "kwargs": kwargs,
            "priority": angel_one.get_angel_call_priority(),
        })
        return {
            "best_strategy": "BUY CE",
            "signal_lifecycle": "CONFIRMED_CALL",
        }

    monkeypatch.setattr(
        history_collector, "_wait_for_option_chain_foreground_grace", no_grace
    )
    monkeypatch.setattr(strategy, "strike_recommendation", fake_recommendation)
    previous_priority = angel_one.get_angel_call_priority()

    ok = asyncio.run(
        history_collector._run_strategy_recommendation_for_capture(
            analyzer, ai, "NIFTY"
        )
    )

    assert ok is True
    assert len(calls) == 1
    assert calls[0]["kwargs"] == {
        "symbol": "NIFTY",
        "expiry": None,
        "cache_bust": "",
        "analyzer": analyzer,
        "ai": ai,
    }
    assert calls[0]["priority"] == angel_one.ANGEL_PRIORITY_BACKGROUND
    assert angel_one.get_angel_call_priority() == previous_priority


def test_capture_once_handles_route_error_and_restores_priority(monkeypatch):
    from app.api.routes import strategy

    async def no_grace():
        return None

    async def fail_recommendation(**kwargs):
        raise RuntimeError("simulated scheduler test failure")

    monkeypatch.setattr(
        history_collector, "_wait_for_option_chain_foreground_grace", no_grace
    )
    monkeypatch.setattr(strategy, "strike_recommendation", fail_recommendation)
    previous_priority = angel_one.get_angel_call_priority()

    ok = asyncio.run(
        history_collector._run_strategy_recommendation_for_capture(
            object(), object(), "SENSEX"
        )
    )

    assert ok is False
    assert angel_one.get_angel_call_priority() == previous_priority


def test_paper_capture_scheduler_rotates_symbols(monkeypatch):
    settings = SimpleNamespace(
        history_collector_symbols=["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
    )
    monkeypatch.setattr(history_collector, "settings", settings)
    monkeypatch.setattr(history_collector, "AIEngine", lambda: object())
    monkeypatch.setattr(history_collector, "is_market_hours_ist", lambda: True)
    monkeypatch.setattr(history_collector, "PAPER_CAPTURE_START_DELAY_SEC", 0.0)
    monkeypatch.setattr(
        history_collector, "PAPER_CAPTURE_ROTATION_INTERVAL_SEC", 15.0
    )

    seen = []

    async def fake_capture(analyzer, ai, symbol):
        seen.append(symbol)
        return True

    monkeypatch.setattr(
        history_collector, "_run_strategy_recommendation_for_capture", fake_capture
    )

    sleeps = []

    async def controlled_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(history_collector.asyncio, "sleep", controlled_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(history_collector.run_periodic_paper_capture(object()))

    assert seen == ["NIFTY", "BANKNIFTY"]
    assert len(sleeps) == 3


def test_paper_capture_scheduler_does_not_evaluate_when_market_closed(monkeypatch):
    settings = SimpleNamespace(history_collector_symbols=["NIFTY"])
    monkeypatch.setattr(history_collector, "settings", settings)
    monkeypatch.setattr(history_collector, "AIEngine", lambda: object())
    monkeypatch.setattr(history_collector, "is_market_hours_ist", lambda: False)
    monkeypatch.setattr(history_collector, "PAPER_CAPTURE_START_DELAY_SEC", 0.0)

    calls = []

    async def fake_capture(analyzer, ai, symbol):
        calls.append(symbol)
        return True

    monkeypatch.setattr(
        history_collector, "_run_strategy_recommendation_for_capture", fake_capture
    )

    sleeps = []

    async def controlled_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(history_collector.asyncio, "sleep", controlled_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(history_collector.run_periodic_paper_capture(object()))

    assert calls == []
    assert sleeps == [0.0, history_collector.PAPER_CAPTURE_CLOSED_RECHECK_SEC]
