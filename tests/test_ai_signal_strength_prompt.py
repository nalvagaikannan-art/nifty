from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[1] / "app/services/ai_engine.py"
).read_text()


def test_ai_prompt_labels_signal_strength_as_score_not_probability():
    assert (
        "- Signal Strength: {decision.get('confidence',50)}/100 "
        "(rule-engine score; not a win probability)"
    ) in SOURCE
    assert "say 'Signal Strength N/100'" in SOURCE
    assert "do not describe it as 'confidence N%'" in SOURCE
    assert "as probability of success" in SOURCE
