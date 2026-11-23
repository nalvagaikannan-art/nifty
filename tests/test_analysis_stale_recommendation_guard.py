from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "templates"
    / "analysis.html"
)
HIDE_GUARD = "$('#rec-option-card,#rec-option-unavailable').hide();"


def test_old_recommendation_is_hidden_when_new_selection_is_queued():
    source = TEMPLATE.read_text(encoding="utf-8")
    load_start = source.index("function loadAll(sym, force, expiry){")
    queue_start = source.index("if(analysisLoadInFlight){", load_start)
    same_poll_return = source.index(
        "if(!force && sameAsFlight) return;", queue_start
    )
    hide_guard = source.index(HIDE_GUARD, same_poll_return)
    pending_assignment = source.index("analysisPendingLoad = {", hide_guard)
    regular_load_start = source.index(
        "var loadSeq = ++analysisLoadSeq;", pending_assignment
    )

    assert (
        queue_start
        < same_poll_return
        < hide_guard
        < pending_assignment
        < regular_load_start
    )


def test_old_recommendation_is_hidden_before_new_network_requests():
    source = TEMPLATE.read_text(encoding="utf-8")
    load_start = source.index("function loadAll(sym, force, expiry){")
    regular_load_start = source.index(
        "var loadSeq = ++analysisLoadSeq;", load_start
    )
    cache_return = source.index(
        "if(!force && apiCache[key])", regular_load_start
    )
    hide_guard = source.index(HIDE_GUARD, cache_return)
    refresh_state = source.index("setRefreshState(true);", hide_guard)
    ai_request = source.index(
        "$.get('/api/analysis/ai/'+sym+cacheQs)", refresh_state
    )
    strategy_request = source.index(
        "$.get('/api/strategy/recommend/'+sym+cacheQs)", ai_request
    )

    assert cache_return < hide_guard < refresh_state < ai_request
    assert ai_request < strategy_request
    assert source.count(HIDE_GUARD) >= 2


def test_expiry_change_invalidates_in_flight_response_and_replays_latest_selection():
    source = TEMPLATE.read_text(encoding="utf-8")

    expiry_handler_start = source.index(
        "$('#expiry-select').on('change',function(){"
    )
    expiry_handler_end = source.index("});", expiry_handler_start)
    expiry_handler = source[expiry_handler_start:expiry_handler_end]
    assert "loadAll(curSym,true,$(this).val());" in expiry_handler

    load_start = source.index("function loadAll(sym, force, expiry){")
    load_end = source.index("\n// SAFETY UI GUARD:", load_start)
    load = source[load_start:load_end]

    assert "curExpiry = expiry;" in load
    assert "expiry === analysisInFlightExpiry;" in load

    pending_assignment = load.index("analysisPendingLoad = {")
    sequence_bump = load.index("analysisLoadSeq++;", pending_assignment)
    assert sequence_bump > pending_assignment

    # Old AI and strategy success/failure callbacks must reject stale responses.
    stale_response_guard = (
        "if(loadSeq !== analysisLoadSeq || loadSym !== curSym) return;"
    )
    assert load.count(stale_response_guard) >= 4

    both_done = load.index("if(done.ai && done.strat)")
    pending_read = load.index("var pending = analysisPendingLoad;", both_done)
    pending_replay = load.index(
        "loadAll(pending.sym, pending.force, pending.expiry);",
        pending_read,
    )
    assert both_done < pending_read < pending_replay
