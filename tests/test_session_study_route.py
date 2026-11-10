import asyncio

import app.api.routes.accuracy as accuracy
from app.schemas import SessionStudyResponse


def test_session_study_route_uses_read_only_service(monkeypatch):
    calls = []

    async def fake_compute(symbol, days=30):
        calls.append((symbol, days))
        return {
            "symbol": symbol,
            "days": days,
            "basis": "Read-only Session Study; no future snapshot is used.",
            "market_days": 1,
            "opening_profile": {},
            "closing_window": {},
            "late_session": {},
        }

    monkeypatch.setattr(accuracy, "compute_session_study", fake_compute)

    result = asyncio.run(
        accuracy.session_study("NIFTY", days=30)
    )

    assert calls == [("NIFTY", 30)]

    validated = SessionStudyResponse(**result)

    assert validated.symbol == "NIFTY"
    assert validated.days == 30
    assert "no future snapshot is used" in validated.basis.lower()
