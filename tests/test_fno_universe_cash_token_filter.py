from app.services.angel_one import _verified_nse_fno_stock_symbols


def test_fno_universe_requires_matching_nse_cash_eq_token():
    instruments = [
        {"exch_seg": "NFO", "instrumenttype": "OPTSTK", "name": "RELIANCE"},
        {"exch_seg": "NFO", "instrumenttype": "FUTSTK", "name": "INFY"},
        {"exch_seg": "NFO", "instrumenttype": "OPTSTK", "name": "011NSETEST"},
        {"exch_seg": "NFO", "instrumenttype": "FUTSTK", "name": "021NSETEST"},
        {"exch_seg": "NFO", "instrumenttype": "FUTIDX", "name": "NIFTY"},
        {"exch_seg": "NSE", "symbol": "RELIANCE-EQ", "token": "2885"},
        {"exch_seg": "NSE", "symbol": "INFY-EQ", "token": "1594"},
    ]

    assert _verified_nse_fno_stock_symbols(instruments) == ["INFY", "RELIANCE"]


def test_fno_universe_ignores_cash_rows_without_token_or_wrong_exchange():
    instruments = [
        {"exch_seg": "NFO", "instrumenttype": "OPTSTK", "name": "ABC"},
        {"exch_seg": "NSE", "symbol": "ABC-EQ", "token": ""},
        {"exch_seg": "BSE", "symbol": "ABC-EQ", "token": "123"},
    ]

    assert _verified_nse_fno_stock_symbols(instruments) == []


def test_fno_universe_is_empty_when_no_matching_cash_underlyings():
    instruments = [
        {"exch_seg": "NFO", "instrumenttype": "OPTSTK", "name": "011NSETEST"},
        {"exch_seg": "NFO", "instrumenttype": "FUTSTK", "name": "021NSETEST"},
    ]

    assert _verified_nse_fno_stock_symbols(instruments) == []
