"""
Tests for app/services/strategy_engine.py — specifically that every leg it
returns carries the *exact* live contract's expiry (never a hardcoded
value), sourced from market_data["option_chain"]["expiry"] the same way
every strike/premium number already is.

Context: the Analysis page's "Best Strike" cards and "Strategy Legs" panel
used to show only "24350 CE" with no expiry — with weekly *and* monthly
contracts both live on the same strike, that's ambiguous about which
contract the suggestion is actually for. These tests pin down that the
resolved expiry from this request's option-chain fetch flows all the way
through to every leg StrategyEngine builds.
"""
import pandas as pd
from app.services.strategy_engine import generate_option_strategy


def _opt_df() -> pd.DataFrame:
    # Minimal chain slice: strikes around a 24650 spot, wide enough (23700
    # to 25600 @ 50pt steps) that iron-condor's short (~1.5% OTM) and long
    # (~3.5% OTM) wings land on genuinely different strikes, not just the
    # nearest single-leg/straddle picks near spot.
    strikes = list(range(23700, 25601, 50))
    rows = []
    for s in strikes:
        dist = abs(s - 24650)
        rows.append({
            "strike":  s,
            "ce_ltp":  max(5.0, 300 - dist * 0.3),
            "pe_ltp":  max(5.0, 300 - dist * 0.3),
        })
    return pd.DataFrame(rows)


def _market_data(strategy: str, expiry: str, side: str = "CALL") -> dict:
    return {
        "decision": {"strategy": strategy, "preferred_side": side},
        "spot": {"price": 24650.0},
        "option_chain": {"expiry": expiry, "all_expiries": [expiry, "04-Sep-2025"]},
    }


def test_single_leg_directional_carries_live_expiry():
    md = _market_data("Directional Call Bias", "28-Aug-2025")
    result = generate_option_strategy(md, _opt_df())

    assert result is not None
    assert len(result["legs"]) == 1
    assert result["legs"][0]["expiry"] == "28-Aug-2025"
    assert result["legs"][0]["type"] == "CE"
    # The human-readable reasoning text must name the expiry too — this is
    # exactly the "BUY 24650 CE @ ..." line shown under Strategy Legs.
    assert "28-Aug-2025" in result["reasoning"]


def test_weak_trend_single_leg_carries_live_expiry():
    md = _market_data("Weak-Trend PE Bias — Small Size", "04-Sep-2025", side="PUT")
    result = generate_option_strategy(md, _opt_df())

    assert result is not None
    assert result["legs"][0]["expiry"] == "04-Sep-2025"
    assert result["legs"][0]["type"] == "PE"


def test_straddle_strangle_legs_all_carry_same_live_expiry():
    md = _market_data("Long Straddle / Strangle", "11-Sep-2025")
    result = generate_option_strategy(md, _opt_df())

    assert result is not None
    assert len(result["legs"]) == 2
    assert all(leg["expiry"] == "11-Sep-2025" for leg in result["legs"])


def test_iron_condor_all_four_legs_carry_same_live_expiry():
    md = _market_data("Range Strategy — Iron Condor", "28-Aug-2025")
    result = generate_option_strategy(md, _opt_df())

    assert result is not None
    assert len(result["legs"]) == 4
    assert all(leg["expiry"] == "28-Aug-2025" for leg in result["legs"])


def test_switching_expiry_changes_the_leg_expiry_not_just_the_top_level_field():
    """Same strategy, same opt_df shape — only the resolved expiry differs
    (as it would when the user picks a different expiry on the Analysis
    page). The leg must follow, not stay pinned to whatever was first
    computed."""
    df = _opt_df()
    near = generate_option_strategy(_market_data("Directional Call Bias", "28-Aug-2025"), df)
    far  = generate_option_strategy(_market_data("Directional Call Bias", "25-Sep-2025"), df)

    assert near["legs"][0]["expiry"] == "28-Aug-2025"
    assert far["legs"][0]["expiry"] == "25-Sep-2025"


def test_no_expiry_available_degrades_to_empty_string_not_a_crash():
    """option_chain missing/empty expiry (e.g. a degraded fetch) must not
    blow up strategy generation — legs just carry an empty expiry rather
    than a fabricated one."""
    md = {
        "decision": {"strategy": "Directional Call Bias", "preferred_side": "CALL"},
        "spot": {"price": 24650.0},
        "option_chain": {},
    }
    result = generate_option_strategy(md, _opt_df())

    assert result is not None
    assert result["legs"][0]["expiry"] == ""
