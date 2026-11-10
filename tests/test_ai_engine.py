import pytest
from app.services.ai_engine import AIEngine
from app.exceptions import AIProviderError


def test_ai_engine_requires_api_key(monkeypatch):
    """AIEngine should defer the missing-key error until analyze_market()."""
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "ai_provider", "gemini")

    engine = AIEngine()

    assert engine.order == []


def test_prompt_requires_tamil_script_not_tanglish():
    engine = AIEngine()
    prompt = engine._build_prompt(
        {"symbol": "NIFTY", "spot": {"price": 24000}},
        {"preferred_side": "NONE"},
    )
    assert "using Tamil script" in prompt
    assert "Do NOT write Tanglish" in prompt
