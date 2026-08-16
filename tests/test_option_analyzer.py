"""
Pure-function tests for OptionAnalyzer — hand-built option-chain fixtures,
no network. Covers spec §43's "PCR / Max Pain / OI calculations correct?"
and CODE_REVIEW.md's Max Pain fix (Round 1: was computing "highest OI
strike" and mislabeling it Max Pain).
"""
from app.services.option_analyzer import OptionAnalyzer


def _chain_row(strike, ce_oi, pe_oi, ce_vol=100, pe_vol=100, ce_ltp=10.0, pe_ltp=10.0,
                ce_oi_chg=0, pe_oi_chg=0, ce_iv=15.0, pe_iv=15.0):
    return {
        "strikePrice": strike,
        "CE": {"openInterest": ce_oi, "changeinOpenInterest": ce_oi_chg,
               "totalTradedVolume": ce_vol, "lastPrice": ce_ltp,
               "impliedVolatility": ce_iv, "bidprice": ce_ltp - 0.5, "askPrice": ce_ltp + 0.5},
        "PE": {"openInterest": pe_oi, "changeinOpenInterest": pe_oi_chg,
               "totalTradedVolume": pe_vol, "lastPrice": pe_ltp,
               "impliedVolatility": pe_iv, "bidprice": pe_ltp - 0.5, "askPrice": pe_ltp + 0.5},
    }


def _make_chain(rows):
    return {"data": rows}


def test_process_option_chain_builds_dataframe():
    analyzer = OptionAnalyzer()
    chain = _make_chain([_chain_row(24600, 1000, 2000), _chain_row(24650, 1500, 1200)])
    df = analyzer.process_option_chain(chain)
    assert len(df) == 2
    assert set(df["strike"]) == {24600.0, 24650.0}


def test_process_option_chain_empty_when_no_data():
    analyzer = OptionAnalyzer()
    df = analyzer.process_option_chain({"data": []})
    assert df.empty


def test_compute_pcr_ratio():
    analyzer = OptionAnalyzer()
    # total PE OI = 3000, total CE OI = 1000 → PCR = 3.0 (heavy put writing)
    chain = _make_chain([
        _chain_row(24600, 500, 1500),
        _chain_row(24650, 500, 1500),
    ])
    df = analyzer.process_option_chain(chain)
    assert analyzer.compute_pcr(df) == 3.0


def test_compute_pcr_zero_when_empty():
    analyzer = OptionAnalyzer()
    assert analyzer.compute_pcr(analyzer.process_option_chain({"data": []})) == 0.0


def test_compute_max_pain_picks_minimum_total_writer_loss():
    """
    Classic textbook Max Pain setup: three strikes, OI concentrated such
    that the middle strike minimizes total option-writer payout — verified
    by hand (loss = sum(OI_ce * max(0, strike-actual)) + OI_pe * max(0, actual-strike))
    summed across ALL strikes for each *candidate* actual-settlement price.
    """
    analyzer = OptionAnalyzer()
    chain = _make_chain([
        _chain_row(100, ce_oi=10, pe_oi=100),   # heavy PE OI here
        _chain_row(105, ce_oi=100, pe_oi=100),  # heaviest OI both sides → should be near max pain
        _chain_row(110, ce_oi=100, pe_oi=10),   # heavy CE OI here
    ])
    df = analyzer.process_option_chain(chain)
    max_pain = analyzer.compute_max_pain(df)
    assert max_pain == 105.0


def test_compute_max_pain_zero_when_empty():
    analyzer = OptionAnalyzer()
    assert analyzer.compute_max_pain(analyzer.process_option_chain({"data": []})) == 0.0


def test_compute_oi_change_sums_ce_and_pe():
    analyzer = OptionAnalyzer()
    chain = _make_chain([
        _chain_row(24600, 1000, 1000, ce_oi_chg=200, pe_oi_chg=-50),
        _chain_row(24650, 1000, 1000, ce_oi_chg=100, pe_oi_chg=300),
    ])
    df = analyzer.process_option_chain(chain)
    result = analyzer.compute_oi_change(df)
    assert result["ce_change"] == 300
    assert result["pe_change"] == 250
    assert result["total_change"] == 550


def test_oi_summary_identifies_max_oi_strikes():
    analyzer = OptionAnalyzer()
    chain = _make_chain([
        _chain_row(24600, ce_oi=500, pe_oi=9000),   # PE wall here → support
        _chain_row(24650, ce_oi=8000, pe_oi=500),   # CE wall here → resistance
        _chain_row(24700, ce_oi=100, pe_oi=100),
    ])
    df = analyzer.process_option_chain(chain)
    summary = analyzer.oi_summary(df)
    assert summary["pe_max_oi_strike"] == 24600.0
    assert summary["ce_max_oi_strike"] == 24650.0


def test_oi_summary_empty_when_no_data():
    analyzer = OptionAnalyzer()
    assert analyzer.oi_summary(analyzer.process_option_chain({"data": []})) == {}


def test_compute_option_volume_pcr():
    analyzer = OptionAnalyzer()
    chain = _make_chain([_chain_row(24600, 100, 100, ce_vol=1000, pe_vol=2000)])
    df = analyzer.process_option_chain(chain)
    vol = analyzer.compute_option_volume(df)
    assert vol["total_ce_volume"] == 1000
    assert vol["total_pe_volume"] == 2000
    assert vol["volume_pcr"] == 2.0


def test_pick_candidates_prefers_fresh_buildup_near_atm():
    analyzer = OptionAnalyzer()
    chain = _make_chain([
        _chain_row(24550, ce_oi=500, pe_oi=500, ce_oi_chg=0, pe_oi_chg=0),
        _chain_row(24600, ce_oi=1000, pe_oi=1000, ce_oi_chg=800, pe_oi_chg=50),  # ATM, strong CE buildup
        _chain_row(24650, ce_oi=500, pe_oi=500, ce_oi_chg=0, pe_oi_chg=0),
    ])
    df = analyzer.process_option_chain(chain)
    picks = analyzer.pick_candidates(df, underlying=24600.0)
    assert picks["ce"]["strike"] == 24600.0
    assert picks["ce"]["is_atm"] is True
