from app.services.pro_metrics import iv_rank_percentile, chain_metrics, pcr_bias
from app.services.option_analyzer import OptionAnalyzer
from app.services.commodity_feed import rsi, commodity_impact, commodity_ideas
from app.services.news_sentiment import score_titles
from tests.pro_helpers import make_chain


def test_iv_rank_basic_and_reliability():
    hist = [10, 12, 14, 16, 20] * 8      # 40 samples, lo=10 hi=20
    r = iv_rank_percentile(15, hist, min_samples=30)
    assert r["rank"] == 50.0 and r["reliable"] is True and r["n"] == 40
    assert r["percentile"] == 60.0       # 3 of 5 distinct values (10,12,14) below 15
    short = iv_rank_percentile(15, [10, 20], min_samples=30)
    assert short["reliable"] is False


def test_iv_rank_clamped_and_empty():
    assert iv_rank_percentile(25, [10, 20] * 20)["rank"] == 100.0
    assert iv_rank_percentile(5, [10, 20] * 20)["rank"] == 0.0
    e = iv_rank_percentile(0, [])
    assert e["rank"] is None and e["reliable"] is False


def test_chain_metrics_synthetic():
    df = OptionAnalyzer().process_option_chain(make_chain())
    m = chain_metrics(df, 22500.0)
    assert m["ok"] and m["atm_strike"] == 22500.0 and m["strike_step"] == 50.0
    assert abs(m["pcr"] - 1.0) < 0.05 and m["pcr_bias"] == "NEUTRAL"
    assert m["call_wall"] == 22700.0 and m["put_wall"] == 22300.0
    assert abs(m["max_pain"] - 22500.0) <= 100
    assert m["sigma_move"] == round(m["straddle"] * 1.25, 2)


def test_chain_metrics_empty():
    import pandas as pd
    assert chain_metrics(pd.DataFrame(), 22500)["ok"] is False


def test_pcr_bias():
    assert pcr_bias(1.3) == "BULLISH" and pcr_bias(0.6) == "BEARISH" and pcr_bias(1.0) == "NEUTRAL"


def test_rsi():
    assert rsi([1, 2, 3]) is None
    assert rsi(list(range(1, 40))) == 100.0
    assert rsi([float(x) for x in range(40, 1, -1)]) == 0.0


def test_commodity_impact_and_ideas():
    i = commodity_impact(2.0, beta=-0.4)
    assert i["nifty_impact_pct"] == -0.8
    assert commodity_impact(None) is None
    snap = {"CRUDEOIL": {"change_pct": 1.2, "rsi15": 60}, "GOLD": {"change_pct": 0.4, "rsi15": 55},
            "SILVER": {"change_pct": -0.2, "rsi15": 40}, "USDINR": {"change_pct": 0.2}}
    ideas = commodity_ideas(snap, nifty_change_pct=-0.9)
    syms = {x["symbol"] for x in ideas}
    assert syms == {"CRUDEOIL", "GOLD"} and all(x["executable"] is False for x in ideas)


def test_news_score():
    s = score_titles(["Stocks rally on rate cut hopes", "Oil plunge as war fears fall"])
    assert s["headlines"] == 2 and -1 <= s["score"] <= 1
    assert score_titles([])["score"] == 0.0
