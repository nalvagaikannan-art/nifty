import pytest

from app.services.risk_engine import RiskManager as EngineRiskManager
from app.services.risk_manager import RiskManager as LegacyRiskManager


MANAGERS = [EngineRiskManager, LegacyRiskManager]


def _score(result):
    return result.get("score", result.get("risk_score"))


@pytest.mark.parametrize("manager", MANAGERS)
def test_missing_pcr_does_not_add_extreme_pcr_risk_penalty(manager):
    neutral = manager.assess_risk({
        "vix": 15,
        "pcr": 1.0,
        "trend": "sideways",
    })
    missing = manager.assess_risk({
        "vix": 15,
        "pcr": 0,
        "trend": "sideways",
    })
    invalid = manager.assess_risk({
        "vix": 15,
        "pcr": None,
        "trend": "sideways",
    })

    assert _score(missing) == _score(neutral)
    assert _score(invalid) == _score(neutral)


@pytest.mark.parametrize("manager", MANAGERS)
def test_explicitly_unavailable_pcr_does_not_affect_risk(manager):
    neutral = manager.assess_risk({
        "vix": 15,
        "pcr": 1.0,
        "trend": "sideways",
    })
    unavailable = manager.assess_risk({
        "vix": 15,
        "pcr": 1.5,
        "pcr_quality": {"available": False, "source": "unavailable"},
        "trend": "sideways",
    })

    assert _score(unavailable) == _score(neutral)


@pytest.mark.parametrize("manager", MANAGERS)
@pytest.mark.parametrize("pcr", [0.6, 1.4])
def test_real_extreme_pcr_still_adds_risk_adjustment(manager, pcr):
    neutral = manager.assess_risk({
        "vix": 15,
        "pcr": 1.0,
        "trend": "sideways",
    })
    extreme = manager.assess_risk({
        "vix": 15,
        "pcr": pcr,
        "trend": "sideways",
    })

    assert _score(extreme) == _score(neutral) + 20
