from app.services.sell_signals import generate_sell_signals
from tests.pro_helpers import build_ctx


def types(res):
    return {s["type"] for s in res["signals"]}


def test_unreliable_iv_history_gives_no_signal():
    res = generate_sell_signals(build_ctx(n=5))
    assert res["signals"] == [] and any("IV வரலாறு" in w for w in res["wait_reasons"])


def test_high_iv_range_bound_gives_all_three_structures():
    res = generate_sell_signals(build_ctx(iv_rank=85, adx=15))
    assert {"SHORT_STRANGLE", "SHORT_STRADDLE", "IRON_CONDOR"} <= types(res)
    ic = next(s for s in res["signals"] if s["type"] == "IRON_CONDOR")
    assert ic["defined_risk"] and ic["max_loss_inr"] > 0 and ic["lot_size"] == 65
    ce = next(l for l in ic["legs"] if l["option_type"] == "CE")
    pe = next(l for l in ic["legs"] if l["option_type"] == "PE")
    assert ce["strike"] > 22500 > pe["strike"]
    assert ce["strike"] >= 22700 and pe["strike"] <= 22300      # beyond the OI walls
    # hedges are further OTM than the shorts
    hc = next(l for l in ic["hedge_legs"] if l["option_type"] == "CE")
    assert hc["strike"] > ce["strike"] and hc["side"] == "BUY"


def test_mid_iv_gives_only_iron_condor():
    res = generate_sell_signals(build_ctx(iv_rank=50, adx=15))
    assert types(res) == {"IRON_CONDOR"}


def test_low_iv_rank_no_signal():
    res = generate_sell_signals(build_ctx(iv_rank=20, adx=15))
    assert res["signals"] == [] and any("IV Rank" in w for w in res["wait_reasons"])


def test_trending_market_blocks_selling():
    res = generate_sell_signals(build_ctx(iv_rank=90, adx=32))
    assert res["signals"] == [] and any("range-bound" in w for w in res["wait_reasons"])


def test_expiry_day_only_defined_risk_and_note():
    res = generate_sell_signals(build_ctx(iv_rank=90, adx=15, dte=0))
    assert types(res) == {"IRON_CONDOR"}
    assert "3 PM" in res["signals"][0]["expiry_note"]


def test_high_vix_blocks_naked():
    res = generate_sell_signals(build_ctx(iv_rank=90, adx=15, vix=28))
    assert "SHORT_STRANGLE" not in types(res) and "SHORT_STRADDLE" not in types(res)


def test_covered_call_is_advisory_only():
    ctx = build_ctx(iv_rank=75, adx=15)
    ctx["holdings_lots"] = 2
    res = generate_sell_signals(ctx)
    cc = next(s for s in res["signals"] if s["type"] == "COVERED_CALL")
    assert cc["executable"] is False and cc["hedge_legs"] == []


def test_signal_ids_stable_and_unique():
    a = generate_sell_signals(build_ctx(iv_rank=85, adx=15))
    b = generate_sell_signals(build_ctx(iv_rank=85, adx=15))
    ids = [s["id"] for s in a["signals"]]
    assert ids == [s["id"] for s in b["signals"]] and len(ids) == len(set(ids))


def test_proxy_range_bound_without_adx():
    res = generate_sell_signals(build_ctx(iv_rank=85, adx=None))
    assert "IRON_CONDOR" in types(res)          # synthetic chain: max pain ~ spot, PCR ~ 1
