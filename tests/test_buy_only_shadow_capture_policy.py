"""BUY-only policy for new forward-paper capture; SELL remains analysis-only."""

import pytest

from app.api.routes.strategy import _forward_paper_capture_eligible


@pytest.mark.parametrize("action,lifecycle", [
    ("BUY CE", "CONFIRMED_CALL"),
    ("BUY PE", "CONFIRMED_PUT"),
])
def test_confirmed_buy_actions_are_capture_eligible(action, lifecycle):
    assert _forward_paper_capture_eligible(
        action=action,
        lifecycle_state=lifecycle,
        fresh_entry_allowed=True,
        action_blocked=False,
        signal_action=action,
    ) is True


@pytest.mark.parametrize("action,lifecycle", [
    ("SELL CE", "CONFIRMED_PUT"),
    ("SELL PE", "CONFIRMED_CALL"),
])
def test_sell_actions_remain_analysis_only(action, lifecycle):
    assert _forward_paper_capture_eligible(
        action=action,
        lifecycle_state=lifecycle,
        fresh_entry_allowed=True,
        action_blocked=False,
        signal_action=action,
    ) is False
