from app.api.routes.strategy import _score_candidates


def test_hard_gate_forces_wait_without_fabricating_100_score():
    market_data = {
        "decision": {
            "bull_score": 80,
            "bear_score": 10,
            "margin": 30,
            "preferred_side": "CALL",
            "confidence": 80,
            "hard_gated": True,
        },
        "spot": {"price": 22000, "market_open": False},
        "technicals": {"adx": 30, "atr": 25},
        "vix": 12,
        "pcr": 1.2,
        "oi_summary": {
            "ce_max_oi_strike": 23000,
            "pe_max_oi_strike": 21000,
            "atm_iv": 15,
        },
    }

    result = _score_candidates(market_data)

    assert result["best"] == "WAIT"
    assert result["candidates"]["BUY CE"] > result["candidates"]["WAIT"]
    assert result["candidates"]["WAIT"] == 50
    assert result["best_score"] == 50

def test_closed_market_wait_ignores_stale_indicator_additions():
    market_data = {
        "decision": {
            "bull_score": 10,
            "bear_score": 10,
            "margin": 0,
            "preferred_side": "NONE",
            "confidence": 30,
            "hard_gated": True,
        },
        "spot": {"price": 22520.45, "market_open": False},
        "technicals": {"adx": 10, "atr": 25},
        "vix": 15,
        "pcr": 1.0,
        "oi_summary": {
            "ce_max_oi_strike": 23000,
            "pe_max_oi_strike": 22000,
            "atm_iv": 15,
        },
    }

    result = _score_candidates(market_data)

    assert result["best"] == "WAIT"
    assert result["candidates"]["WAIT"] == 50
    assert result["best_score"] == 50


def test_market_open_wait_scoring_retains_existing_components():
    market_data = {
        "decision": {
            "bull_score": 10,
            "bear_score": 10,
            "margin": 0,
            "preferred_side": "NONE",
            "confidence": 30,
            "hard_gated": False,
        },
        "spot": {"price": 22520.45, "market_open": True},
        "technicals": {"adx": 10, "atr": 25},
        "vix": 15,
        "pcr": 1.0,
        "oi_summary": {
            "ce_max_oi_strike": 23000,
            "pe_max_oi_strike": 22000,
            "atm_iv": 15,
        },
    }

    result = _score_candidates(market_data)

    assert result["candidates"]["WAIT"] == 65



def test_missing_pcr_vix_do_not_change_candidate_scores():
    from app.api.routes.strategy import _score_candidates

    common = {
        "decision": {
            "bull_score": 40, "bear_score": 30, "margin": 10,
            "preferred_side": "CALL", "confidence": 50,
        },
        "technicals": {"adx": 15, "atr": 2},
        "spot": {"price": 22000, "market_open": True},
        "oi_summary": {
            "atm_iv": 0, "ce_max_oi_strike": 0, "pe_max_oi_strike": 0,
        },
    }
    missing = dict(common, pcr=0, vix=0)
    neutral = dict(common, pcr=1.0, vix=20.0)

    missing_scores = _score_candidates(missing)["candidates"]
    neutral_scores = _score_candidates(neutral)["candidates"]
    assert missing_scores == neutral_scores
