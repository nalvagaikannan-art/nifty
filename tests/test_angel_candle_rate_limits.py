import asyncio
import time
from types import SimpleNamespace

import pytest

from app.services.angel_one import (
    ANGEL_PRIORITY_BACKGROUND,
    ANGEL_PRIORITY_NORMAL,
    AngelOneError,
    AngelOneSession,
    reset_angel_call_priority,
    set_angel_call_priority,
)


@pytest.mark.parametrize("delivery", ["exception", "status_false_response"])
@pytest.mark.parametrize(
    "priority",
    [ANGEL_PRIORITY_NORMAL, ANGEL_PRIORITY_BACKGROUND],
)
def test_candle_rate_limit_handling(delivery, priority, monkeypatch):
    async def scenario():
        session = AngelOneSession()
        calls = []

        def fake_get_candle_data(params):
            calls.append(params)
            if delivery == "exception":
                raise RuntimeError("AB1021: Exceeding access rate")
            return {
                "status": False,
                "errorcode": "AB1021",
                "message": "Exceeding access rate",
            }

        session._obj = SimpleNamespace(
            getCandleData=fake_get_candle_data
        )

        async def no_throttle(*, call_name):
            return None

        monkeypatch.setattr(session, "_throttle", no_throttle)
        priority_token = set_angel_call_priority(priority)

        try:
            with pytest.raises(AngelOneError, match="rate limit"):
                await session._fetch_candles(
                    exchange="NFO",
                    token="123",
                    interval="FIVE_MINUTE",
                    from_date="2026-10-16 09:15",
                    to_date="2026-10-16 10:00",
                    label="NIFTY FUT",
                    fail_fast_rate_limit=(
                        priority == ANGEL_PRIORITY_NORMAL
                    ),
                )
        finally:
            reset_angel_call_priority(priority_token)

        # Every tested branch should fail fast instead of sending
        # another candle request during a rate-limit event.
        assert len(calls) == 1

        # Any broker rate-limit event protects subsequent calls sharing
        # this session. Background requests still fail fast without retry.
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())

def _stub_session_setup(session, monkeypatch):
    async def no_ensure_session():
        return None

    async def no_throttle(*args, **kwargs):
        return None

    monkeypatch.setattr(session, "ensure_session", no_ensure_session)
    monkeypatch.setattr(session, "_throttle", no_throttle)


@pytest.mark.parametrize("delivery", ["exception", "status_false_response", "string_response"])
def test_shared_rate_limit_cooldown_across_angel_endpoints(delivery, monkeypatch):
    async def scenario():
        session = AngelOneSession()
        calls = []

        def broker_call(*args, **kwargs):
            calls.append((args, kwargs))
            if delivery == "exception":
                raise RuntimeError("AB1021: Exceeding access rate")
            if delivery == "string_response":
                return "AB1021: Exceeding access rate"
            return {
                "status": False,
                "errorcode": "AB1021",
                "message": "Exceeding access rate",
            }

        session._obj = SimpleNamespace(ltpData=broker_call)
        _stub_session_setup(session, monkeypatch)

        with pytest.raises(AngelOneError, match="rate limit"):
            await session.get_ltp("NIFTY")

        assert len(calls) == 1
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["exception", "status_false_response"])
def test_futures_rate_limit_does_not_send_ltp_fallback(delivery, monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            AngelOneSession,
            "is_configured",
            property(lambda _self: True),
        )
        session = AngelOneSession()
        primary_calls = []
        fallback_calls = []

        def fake_market_data(**kwargs):
            primary_calls.append(kwargs)
            if delivery == "exception":
                raise RuntimeError("AB1021: Exceeding access rate")
            return {
                "status": False,
                "errorcode": "AB1021",
                "message": "Exceeding access rate",
            }

        def fake_ltp_data(*args, **kwargs):
            fallback_calls.append((args, kwargs))
            return {"status": True, "data": {"ltp": 100}}

        async def resolve_futures_token(_symbol):
            return {
                "token": "123",
                "exchange": "NFO",
                "expiry": "29OCT2026",
                "tradingsymbol": "NIFTY-FUT",
            }

        session._obj = SimpleNamespace(
            getMarketData=fake_market_data,
            ltpData=fake_ltp_data,
        )
        session._bundled_futures_quote_cache = {}
        monkeypatch.setattr(session, "_resolve_futures_token", resolve_futures_token)
        _stub_session_setup(session, monkeypatch)

        result = await session.get_futures_ltp("NIFTY")
        assert result is None
        assert len(primary_calls) == 1
        assert fallback_calls == []
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["exception", "status_false_response"])
def test_option_chain_rate_limit_sets_shared_cooldown(delivery, monkeypatch):
    async def scenario():
        from datetime import date, timedelta

        session = AngelOneSession()
        expiry = (date.today() + timedelta(days=10)).strftime("%d%b%Y").upper()
        session._instruments = [
            {
                "name": "NIFTY",
                "instrumenttype": "OPTIDX",
                "exch_seg": "NFO",
                "expiry": expiry,
                "strike": "10000.000000",
                "token": "1001",
                "symbol": f"NIFTY{expiry}100CE",
            },
            {
                "name": "NIFTY",
                "instrumenttype": "OPTIDX",
                "exch_seg": "NFO",
                "expiry": expiry,
                "strike": "10000.000000",
                "token": "1002",
                "symbol": f"NIFTY{expiry}100PE",
            },
        ]
        calls = []

        async def no_ensure_instruments():
            return None

        async def no_futures_token(_symbol):
            return None

        def fake_market_data(**kwargs):
            calls.append(kwargs)
            if delivery == "exception":
                raise RuntimeError("AB1021: Exceeding access rate")
            return {
                "status": False,
                "errorcode": "AB1021",
                "message": "Exceeding access rate",
            }

        session._obj = SimpleNamespace(getMarketData=fake_market_data)
        monkeypatch.setattr(session, "_ensure_instruments", no_ensure_instruments)
        monkeypatch.setattr(session, "_resolve_futures_token", no_futures_token)
        _stub_session_setup(session, monkeypatch)

        with pytest.raises(AngelOneError, match="rate limit"):
            await session.get_option_chain(
                "NIFTY",
                expiry=expiry,
                strikes_each_side=None,
                spot_price=100,
            )

        assert len(calls) == 1
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["exception", "status_false_response"])
def test_india_vix_rate_limit_sets_shared_cooldown(delivery, monkeypatch):
    async def scenario():
        session = AngelOneSession()
        calls = []

        def fake_ltp(*args):
            calls.append(args)
            if delivery == "exception":
                raise RuntimeError("AB1021: Exceeding access rate")
            return {
                "status": False,
                "errorcode": "AB1021",
                "message": "Exceeding access rate",
            }

        session._obj = SimpleNamespace(ltpData=fake_ltp)
        _stub_session_setup(session, monkeypatch)

        with pytest.raises(AngelOneError, match="rate limit"):
            await session.get_india_vix()

        assert len(calls) == 1
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["exception", "status_false_response"])
def test_positions_rate_limit_sets_shared_cooldown(delivery, monkeypatch):
    async def scenario():
        session = AngelOneSession()
        calls = []

        def fake_position():
            calls.append(True)
            if delivery == "exception":
                raise RuntimeError("AB1021: Exceeding access rate")
            return {
                "status": False,
                "errorcode": "AB1021",
                "message": "Exceeding access rate",
            }

        session._obj = SimpleNamespace(position=fake_position)
        _stub_session_setup(session, monkeypatch)

        with pytest.raises(AngelOneError, match="Exceeding access rate"):
            await session.get_positions()

        assert len(calls) == 1
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())

@pytest.mark.parametrize("initial_delivery", ["string_response", "status_false_response"])
def test_get_ltp_retry_rate_limit_sets_cooldown(initial_delivery, monkeypatch):
    async def scenario():
        session = AngelOneSession()
        calls = []

        def fake_ltp(*args):
            calls.append(args)
            if len(calls) == 1:
                if initial_delivery == "string_response":
                    return "Invalid Token"
                return {
                    "status": False,
                    "errorcode": "AG8001",
                    "message": "Invalid Token",
                }
            if initial_delivery == "string_response":
                return "AB1021: Exceeding access rate"
            return {
                "status": False,
                "errorcode": "AB1021",
                "message": "Exceeding access rate",
            }

        async def recovered(*args, **kwargs):
            return True

        session._obj = SimpleNamespace(ltpData=fake_ltp)
        _stub_session_setup(session, monkeypatch)
        monkeypatch.setattr(session, "_recover_invalid_token", recovered)

        with pytest.raises(AngelOneError, match="rate limit"):
            await session.get_ltp("NIFTY")

        assert len(calls) == 2
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", ["exception", "status_false_response"])
def test_india_vix_retry_rate_limit_sets_cooldown(delivery, monkeypatch):
    async def scenario():
        session = AngelOneSession()
        calls = []

        def fake_ltp(*args):
            calls.append(args)
            if len(calls) == 1:
                if delivery == "exception":
                    raise RuntimeError("AG8001: Invalid Token")
                return {
                    "status": False,
                    "errorcode": "AG8001",
                    "message": "Invalid Token",
                }

            if delivery == "exception":
                raise RuntimeError("AB1021: Exceeding access rate")
            return {
                "status": False,
                "errorcode": "AB1021",
                "message": "Exceeding access rate",
            }

        async def recovered(*args, **kwargs):
            return True

        session._obj = SimpleNamespace(ltpData=fake_ltp)
        _stub_session_setup(session, monkeypatch)
        monkeypatch.setattr(session, "_recover_invalid_token", recovered)

        with pytest.raises(AngelOneError):
            await session.get_india_vix()

        assert len(calls) == 2
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())


def test_futures_ltp_fallback_retry_rate_limit_sets_cooldown(monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            AngelOneSession,
            "is_configured",
            property(lambda _self: True),
        )
        session = AngelOneSession()
        calls = []

        def fake_ltp(*args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("Invalid Token")
            raise RuntimeError("AB1021: Exceeding access rate")

        async def resolve_futures_token(_symbol):
            return {
                "token": "123",
                "exchange": "NFO",
                "expiry": "29OCT2026",
                "tradingsymbol": "NIFTY-FUT",
            }

        async def recovered(*args, **kwargs):
            return True

        session._obj = SimpleNamespace(ltpData=fake_ltp)
        session._bundled_futures_quote_cache = {}
        monkeypatch.setattr(session, "_resolve_futures_token", resolve_futures_token)
        monkeypatch.setattr(session, "_recover_invalid_token", recovered)
        _stub_session_setup(session, monkeypatch)

        result = await session.get_futures_ltp("NIFTY")
        assert result is None
        assert len(calls) == 2
        assert session._rate_limit_cooldown_until >= time.time() + 7.5

    asyncio.run(scenario())

