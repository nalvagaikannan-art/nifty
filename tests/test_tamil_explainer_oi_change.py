from app.services.tamil_explainer import build_tamil_indicators


def oi_indicator(data):
    items = build_tamil_indicators(data, {})
    return next(item for item in items if item["id"] == "oi_change")


def test_tracked_oi_values_override_legacy_provider_values():
    item = oi_indicator({
        "oi_change": {"ce_change": 0, "pe_change": 0},
        "oi_change_tracked": {
            "available": True,
            "ce_oi_change": -130910,
            "pe_oi_change": 1584895,
        },
    })

    assert item["value_display"] == "CE -130,910 / PE +1,584,895"
    assert item["direction"] == "bull"
    assert "+1,584,895" in item["explanation_ta"]


def test_unavailable_tracked_oi_does_not_fall_back_to_legacy_values():
    item = oi_indicator({
        "oi_change": {"ce_change": 100, "pe_change": 999},
        "oi_change_tracked": {
            "available": False,
            "status": "unavailable",
            "reason": "reference coverage below threshold",
        },
    })

    assert item["value_display"] == "N/A"
    assert item["direction"] == "neutral"
    assert "கிடைக்கவில்லை" in item["explanation_ta"]
    assert "ஆதாரமாகக் கருதக்கூடாது" in item["explanation_ta"]


def test_incomplete_tracked_oi_is_reported_as_unavailable():
    item = oi_indicator({
        "oi_change_tracked": {
            "available": True,
            "ce_oi_change": 10,
            "pe_oi_change": None,
        },
        "oi_change": {"ce_change": 0, "pe_change": 0},
    })

    assert item["value_display"] == "N/A"
    assert "கிடைக்கவில்லை" in item["explanation_ta"]


def test_legacy_oi_is_used_only_when_tracked_contract_is_absent():
    item = oi_indicator({
        "oi_change": {"ce_change": 100, "pe_change": 200},
    })

    assert item["value_display"] == "CE +100 / PE +200"
    assert item["direction"] == "bull"


def test_valid_tracked_zero_changes_remain_a_valid_zero_reading():
    item = oi_indicator({
        "oi_change_tracked": {
            "available": True,
            "ce_oi_change": 0,
            "pe_oi_change": 0,
        },
        "oi_change": {"ce_change": 500, "pe_change": 900},
    })

    assert item["value_display"] == "CE +0 / PE +0"
    assert item["direction"] == "neutral"
    assert "CE/PE Open Interest மாற்றம்" in item["explanation_ta"]
