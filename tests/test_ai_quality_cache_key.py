import pytest
from types import SimpleNamespace
import app.utils.ai_result_cache as c


def q(oi=True, vol=True, oi_pct=95.0, vol_pct=90.0):
    return {
        "available": True,
        "status": "adequate" if oi and vol else "limited",
        "oi_usable": oi, "oi_coverage_pct": oi_pct,
        "volume_usable": vol, "volume_coverage_pct": vol_pct,
        "reason": None if oi else "OI coverage low",
        "volume_reason": None if vol else "Volume coverage low",
    }


def key(monkeypatch, data):
    monkeypatch.setattr(c, "time", SimpleNamespace(time=lambda: 1000.0))
    return c._make_key("NIFTY", "27OCT2026", data)


def test_top_level_and_nested_quality_share_key(monkeypatch):
    base = {"option_chain_valid": True, "option_chain_rows": 42}
    a = {**base, "option_chain_quality": q()}
    b = {**base, "pcr_quality": {"chain_quality": q()}}
    assert key(monkeypatch, a) == key(monkeypatch, b)


@pytest.mark.parametrize("field,a,b", [
    ("oi_usable", True, False),
    ("volume_usable", True, False),
    ("oi_coverage_pct", 95.0, 84.5),
    ("volume_coverage_pct", 90.0, 72.5),
])
def test_quality_change_changes_key(monkeypatch, field, a, b):
    first, second = q(), q()
    first[field], second[field] = a, b
    if field in ("oi_usable", "volume_usable"):
        first["status"] = "adequate"
        second["status"] = "limited"
    base = {"option_chain_valid": True, "option_chain_rows": 42}
    ka = key(monkeypatch, {**base, "option_chain_quality": first})
    kb = key(monkeypatch, {**base, "option_chain_quality": second})
    assert ka != kb


def test_chain_row_count_changes_key(monkeypatch):
    a = {"option_chain_valid": True, "option_chain_rows": 42}
    b = {"option_chain_valid": True, "option_chain_rows": 40}
    assert key(monkeypatch, a) != key(monkeypatch, b)


def test_missing_quality_metadata_is_legacy_deterministic(monkeypatch):
    a = {"option_chain_valid": False, "option_chain_rows": 0}
    b = {**a, "option_chain_quality": {},
         "pcr_quality": {"available": False}}
    assert key(monkeypatch, a) == key(monkeypatch, b)


def prompt_data():
    return {
        "spot": {
            "price": 22716.0,
            "change_percent": -0.40,
            "market_open": True,
        },
        "pcr": 0.980,
        "max_pain": 22750,
        "oi_summary": {
            "max_call_oi_strike": 23000,
            "max_put_oi_strike": 22500,
            "call_oi_change": 1000000,
            "observed_at": "2026-10-22T06:10:00Z",
        },
        "multi_timeframe": {
            "5m": {"trend": "SIDEWAYS"},
            "15m": {"trend": "SIDEWAYS"},
            "1h": {"trend": "DOWN"},
        },
        "decision": {
            "signal_action": "WAIT",
            "signal_strength": 30,
            "hard_gated": True,
        },
    }


def test_material_spot_move_changes_key(monkeypatch):
    first = prompt_data()
    second = prompt_data()
    second["spot"]["price"] = 22725.0
    assert key(monkeypatch, first) != key(monkeypatch, second)


def test_small_spot_move_within_bucket_reuses_key(monkeypatch):
    first = prompt_data()
    second = prompt_data()
    second["spot"]["price"] = 22719.0
    assert key(monkeypatch, first) == key(monkeypatch, second)


def test_pcr_or_max_pain_change_changes_key(monkeypatch):
    first = prompt_data()
    changed_pcr = prompt_data()
    changed_pcr["pcr"] = 0.990
    changed_pain = prompt_data()
    changed_pain["max_pain"] = 22800

    assert key(monkeypatch, first) != key(monkeypatch, changed_pcr)
    assert key(monkeypatch, first) != key(monkeypatch, changed_pain)


def test_oi_summary_change_changes_key(monkeypatch):
    first = prompt_data()
    second = prompt_data()
    second["oi_summary"]["call_oi_change"] = 1100000
    assert key(monkeypatch, first) != key(monkeypatch, second)


def test_oi_wall_strike_change_changes_key(monkeypatch):
    first = prompt_data()
    second = prompt_data()
    second["oi_summary"]["max_call_oi_strike"] = 22500
    assert key(monkeypatch, first) != key(monkeypatch, second)


def test_timeframe_direction_change_changes_key(monkeypatch):
    first = prompt_data()
    second = prompt_data()
    second["multi_timeframe"]["1h"]["trend"] = "UP"
    assert key(monkeypatch, first) != key(monkeypatch, second)


def test_observation_timestamp_alone_does_not_change_key(monkeypatch):
    first = prompt_data()
    second = prompt_data()
    second["oi_summary"]["observed_at"] = "2026-10-22T06:11:00Z"
    assert key(monkeypatch, first) == key(monkeypatch, second)

# PROMPT_INPUT_FINGERPRINT_REGRESSION_20261022
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(symbol="BANKNIFTY"), id="symbol"),
        pytest.param(lambda d: d.update(rsi=65.0), id="rsi"),
        pytest.param(lambda d: d.update(macd={"macd": 3.0}), id="macd"),
        pytest.param(
            lambda d: d.update(support_resistance={
                "support": [22600], "resistance": [22800]
            }),
            id="support-resistance",
        ),
        pytest.param(
            lambda d: d["decision"].update(market_bias="BULLISH"),
            id="market-bias",
        ),
        pytest.param(
            lambda d: d["decision"].update(bull_score=50),
            id="bull-score",
        ),
        pytest.param(
            lambda d: d["decision"].update(bear_score=10),
            id="bear-score",
        ),
        pytest.param(
            lambda d: d["decision"].update(bullish_probability=65),
            id="bullish-probability",
        ),
        pytest.param(
            lambda d: d["decision"].update(bearish_probability=35),
            id="bearish-probability",
        ),
        pytest.param(
            lambda d: d["decision"].update(forecast="Bullish"),
            id="forecast",
        ),
        pytest.param(
            lambda d: d["decision"].update(risk="High"),
            id="risk",
        ),
        pytest.param(
            lambda d: d["decision"].update(reasons=["changed reason"]),
            id="rule-reasons",
        ),
    ],
)
def test_prompt_relevant_inputs_change_cache_key(monkeypatch, mutate):
    from copy import deepcopy

    first = prompt_data()
    second = deepcopy(first)
    mutate(second)

    assert key(monkeypatch, first) != key(monkeypatch, second)
