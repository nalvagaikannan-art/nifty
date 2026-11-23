"""
Tests for app.api.routes.strategy._pick_strikes — the function behind the
Analysis page's "Best Strike / Aggressive (OTM) / Conservative (ITM)" cards.

Each picked strike must carry the expiry of the exact contract it came
from (live from the option-chain fetch this request used), so the UI can
show "24400 CE | 28-Aug-2025" instead of a bare "24400 CE" that's
ambiguous whenever the same strike has both a weekly and monthly contract
live at once.
"""
from app.api.routes.strategy import _pick_strikes


def _chain(expiry: str, all_expiries=None) -> dict:
    strikes = list(range(24200, 25101, 50))
    rows = []
    for s in strikes:
        dist = abs(s - 24650)
        ltp = max(5.0, 300 - dist * 0.4)
        rows.append({
            "strikePrice": s,
            "CE": {"lastPrice": ltp, "openInterest": 50000, "totalTradedVolume": 20000, "impliedVolatility": 14.0},
            "PE": {"lastPrice": ltp, "openInterest": 50000, "totalTradedVolume": 20000, "impliedVolatility": 14.0},
        })
    return {"expiry": expiry, "all_expiries": all_expiries or [expiry], "data": rows}


def test_every_picked_call_strike_carries_the_chains_expiry():
    chain = _chain("28-Aug-2025")
    strikes = _pick_strikes(chain, is_call=True, spot=24650.0)

    assert len(strikes) == 3  # Best / Aggressive / Conservative
    for s in strikes:
        assert s["expiry"] == "28-Aug-2025"
        assert s["type"] == "CE"


def test_every_picked_put_strike_carries_the_chains_expiry():
    chain = _chain("04-Sep-2025")
    strikes = _pick_strikes(chain, is_call=False, spot=24650.0)

    assert len(strikes) == 3
    for s in strikes:
        assert s["expiry"] == "04-Sep-2025"
        assert s["type"] == "PE"


def test_picking_from_a_different_expiry_chain_changes_the_strike_expiry():
    """Same strike ladder, weekly vs monthly chain — the picked strike's
    expiry must follow the chain it was actually picked from, not stay
    pinned to whichever expiry was resolved first."""
    weekly  = _pick_strikes(_chain("28-Aug-2025"), is_call=True, spot=24650.0)
    monthly = _pick_strikes(_chain("25-Sep-2025"), is_call=True, spot=24650.0)

    assert weekly[0]["strike"] == monthly[0]["strike"]      # same strike ladder
    assert weekly[0]["expiry"] == "28-Aug-2025"              # different contract...
    assert monthly[0]["expiry"] == "25-Sep-2025"              # ...different expiry


def test_missing_expiry_in_chain_data_is_empty_not_fabricated():
    chain = _chain("")
    strikes = _pick_strikes(chain, is_call=True, spot=24650.0)
    assert all(s["expiry"] == "" for s in strikes)


def test_recommended_option_sell_ce_preserves_sell_levels():
    from app.api.routes.analysis import _pick_recommended_option

    market_data = {
        "spot": {"price": 24650.0},
        "technicals": {"atr": 100.0},
        "option_chain": _chain("31-Dec-2099"),
    }
    rec = _pick_recommended_option(market_data, {}, "SELL CE")

    assert rec["available"] is True
    assert rec["type"] == "CE"
    assert rec["sl"] > rec["entry_price"] > rec["t1"] > rec["t2"] > rec["t3"]


def test_recommended_option_sell_pe_preserves_sell_levels():
    from app.api.routes.analysis import _pick_recommended_option

    market_data = {
        "spot": {"price": 24650.0},
        "technicals": {"atr": 100.0},
        "option_chain": _chain("08-Oct-2026"),
    }
    rec = _pick_recommended_option(market_data, {}, "SELL PE")

    assert rec["available"] is True
    assert rec["type"] == "PE"
    assert rec["sl"] > rec["entry_price"] > rec["t1"] > rec["t2"] > rec["t3"]

def test_picked_strike_preserves_option_quote_timestamps():
    chain = _chain("28-Aug-2025")
    feed_time = "2026-10-09T14:00:00+05:30"
    trade_time = "2026-10-09T13:59:58+05:30"

    for row in chain["data"]:
        row["CE"]["exchangeFeedTime"] = feed_time
        row["CE"]["exchangeTradeTime"] = trade_time

    strikes = _pick_strikes(chain, is_call=True, spot=24650.0)

    assert len(strikes) == 3
    for picked in strikes:
        assert picked["exchange_feed_time"] == feed_time
        assert picked["exchange_trade_time"] == trade_time



def test_picked_strike_uses_complete_fresh_websocket_quote(monkeypatch):
    from types import SimpleNamespace
    import app.api.routes.strategy as strategy_module

    chain = _chain("28-Aug-2025")
    now = 1791870925.0
    monkeypatch.setattr(
        strategy_module, "time", SimpleNamespace(time=lambda: now)
    )

    for row in chain["data"]:
        side = row["CE"]
        live_ltp = side["lastPrice"] + 5.0
        side.update({
            "live_ws": True,
            "live_ws_source": "angel_one_websocket",
            "live_ws_quote": {
                "ltp": live_ltp,
                "bid": live_ltp - 0.5,
                "ask": live_ltp + 0.5,
                "received_at": now - 0.4,
                "source": "angel_one_websocket",
                "exchange_timestamp": "2026-10-13T11:25:25+05:30",
                "exchange_timestamp_ms": 1791870925000,
            },
        })

    picked = _pick_strikes(chain, is_call=True, spot=24650.0)
    assert len(picked) == 3

    for item in picked:
        row = next(r for r in chain["data"] if r["strikePrice"] == item["strike"])
        side = row["CE"]
        live_ltp = side["lastPrice"] + 5.0
        assert item["ltp"] == live_ltp
        assert item["bid"] == live_ltp - 0.5
        assert item["ask"] == live_ltp + 0.5
        assert item["price_source"] == "angel_one_websocket"
        assert item["live_ws"] is True
        assert item["live_ws_received_at"] == now - 0.4
        assert item["live_ws_age_sec"] == 0.4
        assert item["live_ws_exchange_timestamp"] == "2026-10-13T11:25:25+05:30"


def test_picker_does_not_trust_legacy_ws_markers_without_nested_quote(monkeypatch):
    from types import SimpleNamespace
    import app.api.routes.strategy as strategy_module

    chain = _chain("28-Aug-2025")
    now = 1791870925.0
    monkeypatch.setattr(
        strategy_module, "time", SimpleNamespace(time=lambda: now)
    )

    for row in chain["data"]:
        side = row["CE"]
        side["bidprice"] = side["lastPrice"] - 0.5
        side["askPrice"] = side["lastPrice"] + 0.5
        # Simulate old flat markers left by a former quote pipeline.
        side.update({
            "live_ws": True,
            "live_ws_received_at": now - 0.2,
            "live_ws_age_sec": 0.2,
            "live_ws_source": "angel_one_websocket",
            "live_ws_exchange_timestamp": "old-marker-only",
        })

    picked = _pick_strikes(chain, is_call=True, spot=24650.0)
    assert len(picked) == 3

    for item in picked:
        row = next(r for r in chain["data"] if r["strikePrice"] == item["strike"])
        side = row["CE"]
        assert item["ltp"] == side["lastPrice"]
        assert item["bid"] == side["bidprice"]
        assert item["ask"] == side["askPrice"]
        assert item["price_source"] == "rest_unknown"
        assert item["live_ws"] is False
        assert item["live_ws_received_at"] is None
        assert item["live_ws_source"] is None


def test_picker_rejects_incomplete_ws_quote_instead_of_mixing_rest_prices(monkeypatch):
    from types import SimpleNamespace
    import app.api.routes.strategy as strategy_module

    chain = _chain("28-Aug-2025")
    now = 1791870925.0
    monkeypatch.setattr(
        strategy_module, "time", SimpleNamespace(time=lambda: now)
    )

    for row in chain["data"]:
        side = row["CE"]
        side["bidprice"] = side["lastPrice"] - 0.5
        side["askPrice"] = side["lastPrice"] + 0.5
        side.update({
            "live_ws": True,
            "live_ws_source": "angel_one_websocket",
            "live_ws_quote": {
                # Deliberately invalid: ask is absent; do not mix this LTP
                # and bid with REST ask.
                "ltp": side["lastPrice"] + 100.0,
                "bid": side["lastPrice"] + 99.5,
                "received_at": now - 0.2,
                "source": "angel_one_websocket",
            },
        })

    picked = _pick_strikes(chain, is_call=True, spot=24650.0)
    assert len(picked) == 3

    for item in picked:
        row = next(r for r in chain["data"] if r["strikePrice"] == item["strike"])
        side = row["CE"]
        assert item["ltp"] == side["lastPrice"]
        assert item["bid"] == side["bidprice"]
        assert item["ask"] == side["askPrice"]
        assert item["price_source"] == "rest_unknown"
        assert item["live_ws"] is False
