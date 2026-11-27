from app.api.routes.mcx_diagnostic import build_diagnostic


def test_mcx_optfut_diagnostic_is_allowlisted_and_counts_ce_pe():
    payload = {
        "cache_day": "2026-11-27",
        "schema_version": 4,
        "rows": [
            {
                "name": "GOLD", "symbol": "GOLD31DEC26CE", "expiry": "31DEC2026",
                "strike": "125000", "lotsize": "1", "tick_size": "100",
                "instrumenttype": "OPTFUT", "exch_seg": "MCX",
                "token": "MUST_NOT_LEAK",
            },
            {
                "name": "GOLD", "symbol": "GOLD31DEC26PE", "expiry": "31DEC2026",
                "strike": "125000", "lotsize": "1", "tick_size": "100",
                "instrumenttype": "OPTFUT", "exch_seg": "MCX",
                "token": "MUST_NOT_LEAK",
            },
            {
                "name": "GOLD", "symbol": "GOLD31DEC26", "expiry": "31DEC2026",
                "instrumenttype": "FUTCOM", "exch_seg": "MCX",
            },
        ],
    }

    result = build_diagnostic(payload, "2026-11-27")

    assert result["mode"] == "READ_ONLY_EXISTING_CACHE_ONLY"
    assert result["cache_freshness"] == "TODAY"
    assert result["mcx_optfut_row_count"] == 2
    assert result["option_type_counts"] == {"CE": 1, "PE": 1}
    assert result["commodities"][0]["name"] == "GOLD"
    assert result["commodities"][0]["ce"] == 1
    assert result["commodities"][0]["pe"] == 1
    assert result["secrets_or_tokens_included"] is False
    assert all("token" not in sample for item in result["commodities"] for sample in item["samples"])
    assert "MUST_NOT_LEAK" not in str(result)


def test_mcx_optfut_diagnostic_marks_stale_cache():
    result = build_diagnostic(
        {"cache_day": "2026-11-26", "schema_version": 4, "rows": []},
        "2026-11-27",
    )
    assert result["cache_freshness"] == "NOT_TODAY"
    assert result["mcx_optfut_row_count"] == 0
    assert result["quote_support_tested"] is False
