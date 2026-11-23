from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "app/templates/dashboard.html").read_text()
ANALYSIS = (ROOT / "app/templates/analysis.html").read_text()
DATA_FETCHER = (ROOT / "app/services/data_fetcher.py").read_text()
MARKET_ANALYZER = (ROOT / "app/services/market_analyzer.py").read_text()


def test_dashboard_gates_pcr_and_max_pain_on_oi_quality():
    assert "pcrMeta.available !== false && !oiQualityBad" in DASHBOARD
    assert "!oiQualityBad && Number.isFinite(maxPain)" in DASHBOARD


def test_dashboard_exposes_chain_quality_warnings():
    assert "OI Quality Limited" in DASHBOARD
    assert "Volume Quality Limited" in DASHBOARD


def test_analysis_gates_pcr_and_max_pain_on_oi_quality():
    assert "!chainUnavailable && pcrMeta.available !== false && !oiQualityBad" in ANALYSIS
    assert "!chainUnavailable && !oiQualityBad && Number(d.max_pain) > 0" in ANALYSIS


def test_analysis_summary_exposes_oi_and_volume_warnings():
    assert "OI quality limited (' + oiCoverageText" in ANALYSIS
    assert "Volume quality limited (' + volumeCoverageText" in ANALYSIS
    assert "oiQualityBad || volumeQualityBad" in ANALYSIS

def test_dashboard_spot_health_requires_fresh_observation_timestamp():
    assert "Date.parse(d.spot_observed_at || '')" in DASHBOARD
    assert "spotAgeSec >= -5 && spotAgeSec <= 10" in DASHBOARD
    assert "spot: d.market_open === false ? 'delayed' : (spotFresh ? 'live' : 'delayed')" in DASHBOARD

def test_dashboard_coverage_label_explains_metric_scope():
    assert "Signal Data Coverage</span>" in DASHBOARD
    assert (
        'title="Weighted availability of decision-engine inputs; '
        'freshness is shown separately in feed status tiles."'
    ) in DASHBOARD

def test_chain_fetch_timestamp_flows_to_quality_and_dashboard():
    assert MARKET_ANALYZER.count('"fetched_at": chain.get("fetched_at") or None') == 2
    assert "var chainFresh = chainAvailable && healthQualityReported" in DASHBOARD
    assert "chainAgeSec <= 60" in DASHBOARD
    assert "chainFresh ? 'Recent Fetch' : hdText(scores.chain)" in DASHBOARD
    assert "Provider observation timestamp remains unverified." in DASHBOARD
    for source in (
        '"data_source":      "angel_one",',
        '"data_source":      "zerodha",',
        '"data_source":      f"nse_curl_cffi_v3/{IMPERSONATE}",',
        '"data_source":      f"nse_curl_cffi/{IMPERSONATE}",',
    ):
        pos = DATA_FETCHER.index(source)
        assert '"fetched_at": current_ist().isoformat()' in DATA_FETCHER[pos:pos + 180]

