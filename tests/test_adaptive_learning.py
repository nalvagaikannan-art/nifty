from types import SimpleNamespace

from app.services.adaptive_learning import build_shadow_report


def _record(symbol, regime, action, horizon, value, status="matured"):
    ledger = SimpleNamespace(
        symbol=symbol,
        action=action,
        entry_snapshot={"forward_paper_capture": {"market_regime": {"regime": regime}}},
    )
    outcome = SimpleNamespace(
        status=status,
        horizon_minutes=horizon,
        execution_aware_return_pct=value,
        premium_action_adjusted_return_pct=value,
    )
    return ledger, outcome


def test_shadow_report_requires_minimum_sample_and_never_changes_live_weights():
    rows = [_record("NIFTY", "TREND_UP", "BUY CE", 15, 1.0) for _ in range(29)]
    report = build_shadow_report(rows, minimum_samples=30)
    assert report["mode"] == "shadow_only"
    assert report["live_weights_changed"] is False
    assert report["groups"][0]["samples"] == 29
    assert report["groups"][0]["candidate_weight"] is None
    assert report["groups"][0]["status"] == "INSUFFICIENT_SAMPLE"


def test_shadow_report_emits_bounded_candidate_only_after_sample_threshold():
    rows = (
        [_record("BANKNIFTY", "RANGE", "BUY PE", 10, 1.0) for _ in range(24)]
        + [_record("BANKNIFTY", "RANGE", "BUY PE", 10, -1.0) for _ in range(6)]
    )
    report = build_shadow_report(rows, minimum_samples=30)
    group = report["groups"][0]
    assert group["samples"] == 30
    assert group["win_rate_pct"] == 80.0
    assert group["candidate_weight"] == 1.2
    assert group["status"] == "CANDIDATE_SHADOW_ONLY"
    assert report["live_weights_changed"] is False


def test_unknown_regime_and_unmatured_rows_are_excluded():
    unknown = _record("NIFTY", "UNKNOWN", "BUY CE", 5, 1.0)
    pending = _record("NIFTY", "TREND_UP", "BUY CE", 5, 1.0, status="pending")
    report = build_shadow_report([unknown, pending])
    assert report["groups"] == []
    assert report["excluded"]["unknown_regime"] == 1
    assert report["excluded"]["ungraded"] == 1
