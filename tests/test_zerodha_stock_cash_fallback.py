import asyncio

from app.services.zerodha import KiteSession, ZerodhaError


def _session_with_nse_master(rows):
    session = KiteSession()
    session._nse_instruments = rows
    session._nse_instruments_ts = 9999999999
    return session


def test_resolve_exact_nse_cash_eq_token():
    session = _session_with_nse_master([
        {"exchange": "NSE", "segment": "NSE", "instrument_type": "EQ",
         "tradingsymbol": "RELIANCE", "instrument_token": 2885},
        {"exchange": "NSE", "segment": "NSE", "instrument_type": "EQ",
         "tradingsymbol": "INFY", "instrument_token": 1594},
        {"exchange": "NFO", "segment": "NFO", "instrument_type": "CE",
         "tradingsymbol": "RELIANCE", "instrument_token": 123},
    ])
    assert asyncio.run(session._resolve_nse_equity_token("RELIANCE")) == 2885


def test_resolve_nse_cash_token_rejects_missing_or_ambiguous_symbol():
    rows = [
        {"exchange": "NSE", "segment": "NSE", "instrument_type": "EQ",
         "tradingsymbol": "RELIANCE", "instrument_token": 2885},
        {"exchange": "NSE", "segment": "NSE", "instrument_type": "EQ",
         "tradingsymbol": "RELIANCE", "instrument_token": 2886},
    ]
    session = _session_with_nse_master(rows)
    try:
        asyncio.run(session._resolve_nse_equity_token("RELIANCE"))
        assert False, "ambiguous token must fail closed"
    except ZerodhaError:
        pass

    try:
        asyncio.run(session._resolve_nse_equity_token("UNKNOWN"))
        assert False, "missing token must fail closed"
    except ZerodhaError:
        pass


def test_stock_ltp_uses_resolved_cash_token_and_nse_quote_key(monkeypatch):
    session = _session_with_nse_master([
        {"exchange": "NSE", "segment": "NSE", "instrument_type": "EQ",
         "tradingsymbol": "RELIANCE", "instrument_token": 2885},
    ])

    class FakeKite:
        def quote(self, keys):
            assert keys == ["NSE:RELIANCE"]
            return {"NSE:RELIANCE": {
                "last_price": 2500, "ohlc": {
                    "open": 2480, "high": 2510, "low": 2470, "close": 2490
                }
            }}

    session._kite = FakeKite()
    async def no_throttle():
        return None
    monkeypatch.setattr(session, "_throttle", no_throttle)
    result = asyncio.run(session.get_ltp("RELIANCE"))
    assert result["symbol"] == "RELIANCE"
    assert result["price"] == 2500


def test_stock_candles_use_resolved_cash_token(monkeypatch):
    session = _session_with_nse_master([
        {"exchange": "NSE", "segment": "NSE", "instrument_type": "EQ",
         "tradingsymbol": "HDFCBANK", "instrument_token": 1333},
    ])

    class FakeKite:
        def historical_data(self, token, from_date, to_date, interval):
            assert token == 1333
            assert interval == "5minute"
            return [{"date": "2026-11-23 10:00:00", "open": 100, "high": 102,
                     "low": 99, "close": 101, "volume": 1234}]

    session._kite = FakeKite()
    async def no_throttle():
        return None
    monkeypatch.setattr(session, "_throttle", no_throttle)
    result = asyncio.run(session.get_candle_data(
        "HDFCBANK", interval="5minute",
        from_date="2026-11-23 09:15:00", to_date="2026-11-23 10:05:00"
    ))
    assert result[0]["close"] == 101
    assert result[0]["volume"] == 1234
