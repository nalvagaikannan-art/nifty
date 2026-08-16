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
