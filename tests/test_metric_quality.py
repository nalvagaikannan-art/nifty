from app.api.routes.strategy import _metric_quality_payload


def test_missing_vix_is_explicitly_unavailable():
    result = _metric_quality_payload({
        "vix": 0,
        "pcr": 0,
        "option_chain_valid": False,
        "option_chain": {"data": []},
    })
    assert result["vix_quality"]["available"] is False
    assert result["vix_quality"]["source"] == "unavailable"
    assert result["pcr_quality"]["available"] is False
    assert result["option_chain_valid"] is False


def test_verified_vix_and_pcr_metadata_is_preserved():
    md = {
        "vix": 13.45,
        "vix_quality": {
            "available": True,
            "source": "nse_allIndices",
            "fetched_at": "2026-10-14T15:29:00+05:30",
            "observed_at": "14-Oct-2026 15:29:00",
            "reason": None,
        },
        "pcr": 1.12,
        "pcr_quality": {
            "available": True,
            "source": "angel_one",
            "fetched_at": None,
            "observed_at": None,
            "reason": None,
        },
        "option_chain_valid": True,
        "option_chain": {"data_source": "angel_one", "data": [{"CE": {}}]},
    }
    result = _metric_quality_payload(md)
    assert result["vix_quality"]["available"] is True
    assert result["vix_quality"]["source"] == "nse_allIndices"
    assert result["vix_quality"]["observed_at"] == "14-Oct-2026 15:29:00"
    assert result["pcr_quality"]["available"] is True
    assert result["option_chain_valid"] is True


def test_pcr_cannot_be_available_when_chain_is_invalid():
    result = _metric_quality_payload({
        "pcr": 1.25,
        "pcr_quality": {"available": True, "source": "angel_one"},
        "option_chain_valid": False,
        "option_chain": {"data": []},
    })
    assert result["pcr_quality"]["available"] is False


def test_analysis_metric_quality_does_not_claim_missing_vix():
    from app.api.routes.analysis import _metric_quality_fields

    result = _metric_quality_fields({
        "vix": 0,
        "pcr": 0,
        "option_chain_valid": False,
        "option_chain": {"data": []},
    })
    assert result["vix_quality"]["available"] is False
    assert result["pcr_quality"]["available"] is False
    assert result["option_chain_valid"] is False


def test_analysis_metric_quality_preserves_verified_sources():
    from app.api.routes.analysis import _metric_quality_fields

    result = _metric_quality_fields({
        "vix": 13.45,
        "vix_quality": {
            "available": True,
            "source": "nse_allIndices",
            "fetched_at": "2026-10-14T15:29:00+05:30",
            "observed_at": "14-Oct-2026 15:29:00",
            "reason": None,
        },
        "pcr": 1.12,
        "pcr_quality": {
            "available": True,
            "source": "angel_one",
            "reason": None,
        },
        "option_chain_valid": True,
        "option_chain": {
            "data_source": "angel_one",
            "data": [{"CE": {}}],
        },
    })
    assert result["vix_quality"]["available"] is True
    assert result["vix_quality"]["source"] == "nse_allIndices"
    assert result["pcr_quality"]["available"] is True
    assert result["option_chain_valid"] is True
