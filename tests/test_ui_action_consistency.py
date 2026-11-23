from pathlib import Path


def test_analysis_headline_uses_gated_effective_action():
    page = Path("app/templates/analysis.html").read_text(encoding="utf-8")

    assert "var dec = analysisEffectiveStrategy(d);" in page
    assert (
        "var dec = String(d.signal_action || '').toString().toUpperCase().trim();"
        not in page
    )


def test_analysis_effective_action_obeys_entry_and_hard_gates():
    page = Path("app/templates/analysis.html").read_text(encoding="utf-8")
    start = page.index("function analysisEffectiveStrategy(d)")
    end = page.index("\nfunction renderCandidates", start)
    function = page[start:end]

    for token in (
        "d.hard_gated",
        "d.market_regime_no_trade",
        "gate.allowed === false",
        "gate.fresh_entry_allowed === false",
        "lifecycle.startsWith('HOLD_')",
        "lifecycle.startsWith('WATCH_')",
        "d.signal_action",
    ):
        assert token in function, f"Missing action-safety condition: {token}"


def test_dashboard_and_analysis_use_gated_canonical_actions():
    dashboard = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    analysis = Path("app/templates/analysis.html").read_text(encoding="utf-8")

    assert "function ltEffectiveStrategy(d)" in dashboard
    assert "var best = ltEffectiveStrategy(d);" in dashboard
    assert "function analysisEffectiveStrategy(d)" in analysis
    assert "var dec = analysisEffectiveStrategy(d);" in analysis
