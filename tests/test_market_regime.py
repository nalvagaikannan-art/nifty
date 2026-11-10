from datetime import datetime
from app.services import market_regime


def _session_at(monkeypatch, hour, minute):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 3, hour, minute, tzinfo=tz)

    monkeypatch.setattr(market_regime, "datetime", FakeDateTime)
    return market_regime._time_session()


def test_pre_market_before_0915(monkeypatch):
    assert _session_at(monkeypatch, 9, 14) == "PRE_MARKET"


def test_opening_starts_at_0915(monkeypatch):
    assert _session_at(monkeypatch, 9, 15) == "OPENING"


def test_opening_ends_before_0945(monkeypatch):
    assert _session_at(monkeypatch, 9, 44) == "OPENING"


def test_mid_starts_at_0945(monkeypatch):
    assert _session_at(monkeypatch, 9, 45) == "MID"


def test_mid_ends_before_1500(monkeypatch):
    assert _session_at(monkeypatch, 14, 59) == "MID"


def test_closing_starts_at_1500(monkeypatch):
    assert _session_at(monkeypatch, 15, 0) == "CLOSING"


def test_closing_at_1530(monkeypatch):
    assert _session_at(monkeypatch, 15, 30) == "CLOSING"


def test_post_market_after_1530(monkeypatch):
    assert _session_at(monkeypatch, 15, 31) == "POST_MARKET"
