import asyncio


def test_history_bridge_skips_builder_when_history_not_due(monkeypatch):
    from app.api.routes import strategy
    from app.services import history_service

    calls = []

    async def not_due(symbol, snapshot_timestamp=None, interval_minutes=5):
        calls.append((symbol, snapshot_timestamp, interval_minutes))
        return False

    async def forbidden(*args, **kwargs):
        raise AssertionError("builder/save must not run when history is not due")

    monkeypatch.setattr(history_service, "analysis_history_due", not_due)
    monkeypatch.setattr(history_service, "save_analysis_result", forbidden)

    import app.api.routes.analysis as analysis
    monkeypatch.setattr(analysis, "build_ai_analysis", forbidden)

    market_data = {"timestamp": "snap-1", "option_chain": {}}
    asyncio.run(strategy._persist_analysis_history_if_due(
        "NIFTY", object(), object(), market_data, {"symbol": "NIFTY"}
    ))

    assert calls == [("NIFTY", "snap-1", 5)]


def test_history_bridge_reuses_existing_snapshot_and_ai_result(monkeypatch):
    from app.api.routes import analysis, strategy
    from app.services import history_service

    market_data = {
        "timestamp": "snap-2",
        "option_chain": {"expiry": "13OCT2026"},
    }
    state = {"symbol": "BANKNIFTY", "lifecycle": "WATCH_CALL"}
    ai_result = {"reason": "already computed"}
    built = []
    saved = []

    async def due(symbol, snapshot_timestamp=None, interval_minutes=5):
        assert (symbol, snapshot_timestamp, interval_minutes) == (
            "BANKNIFTY", "snap-2", 5
        )
        return True

    async def build(symbol, analyzer, ai, **kwargs):
        built.append((symbol, kwargs))
        return {
            "symbol": symbol,
            "signal_lifecycle": "WATCH_CALL",
            "market_snapshot_timestamp": "snap-2",
        }

    async def save(symbol, kind, result):
        saved.append((symbol, kind, result))

    monkeypatch.setattr(history_service, "analysis_history_due", due)
    monkeypatch.setattr(history_service, "save_analysis_result", save)
    monkeypatch.setattr(analysis, "build_ai_analysis", build)

    asyncio.run(strategy._persist_analysis_history_if_due(
        "BANKNIFTY", object(), object(), market_data, state,
        ai_result=ai_result,
    ))

    assert len(built) == 1
    assert built[0][1]["market_data_override"] is market_data
    assert built[0][1]["ai_result_override"] is ai_result
    assert built[0][1]["persisted_state_override"] is state
    assert len(saved) == 1
    assert saved[0][:2] == ("BANKNIFTY", "ai")
    assert saved[0][2]["market_snapshot_timestamp"] == "snap-2"
