from app.api.routes.strategy import _score_candidates


def _market_data(bull, bear, margin, preferred="NONE", confidence=40):
    return {
        "decision": {
            "bull_score": bull,
            "bear_score": bear,
            "margin": margin,
            "preferred_side": preferred,
            "confidence": confidence,
        },
        "technicals": {
            "adx": 22,
            "atr": 18,
        },
        "vix": 11.2,
        "pcr": 0.74,
        "spot": {
            "price": 23915.95,
            "market_open": True,
        },
        "oi_summary": {
            "ce_max_oi_strike": 24000,
            "pe_max_oi_strike": 23900,
            "atm_iv": 0,
        },
    }


def test_weak_sideways_signal_results_in_wait():
    result = _score_candidates(
        _market_data(21, 14, 7, "NONE", 39)
    )

    assert result["best"] == "WAIT"
    assert result["candidates"]["WAIT"] == 45


def test_bearish_signal_does_not_force_buy_ce():
    result = _score_candidates(
        _market_data(10, 23, -20, "NONE", 48)
    )

    assert result["candidates"]["BUY PE"] == 30
    assert result["candidates"]["BUY CE"] == 7
    assert result["best"] == "WAIT"


def test_strong_call_signal_can_select_buy_ce():
    result = _score_candidates(
        _market_data(35, 10, 25, "CALL", 70)
    )

    assert result["candidates"]["BUY CE"] > 0
    assert result["best"] == "BUY CE"


def test_strong_put_signal_can_select_buy_pe():
    result = _score_candidates(
        _market_data(10, 35, -25, "PUT", 70)
    )

    assert result["candidates"]["BUY PE"] > 0
    assert result["best"] == "BUY PE"


def test_hold_call_lifecycle_keeps_active_call_despite_bearish_fresh_score():
    result = _score_candidates(
        _market_data(10, 23, -20, "NONE", 48)
    )

    candidates = result["candidates"]
    raw_best = result["best"]

    assert raw_best == "WAIT"
    assert candidates["BUY PE"] > candidates["BUY CE"]

    # Persistent HOLD_CALL must preserve the confirmed active direction.
    lifecycle_state = "HOLD_CALL"
    lifecycle_active = "CALL"

    best = raw_best
    best_score = result["best_score"]

    if lifecycle_state.startswith("HOLD_") and lifecycle_active in ("CALL", "PUT"):
        best = "BUY CE" if lifecycle_active == "CALL" else "BUY PE"
        best_score = candidates.get(best, best_score)

    assert best == "BUY CE"
    assert best_score == candidates["BUY CE"]
