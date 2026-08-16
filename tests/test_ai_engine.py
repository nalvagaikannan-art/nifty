import pytest
from app.services.ai_engine import AIEngine
from app.exceptions import AIProviderError


def test_ai_engine_requires_api_key(monkeypatch):
    """AIEngine should raise if no API key is configured for the selected provider."""
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "ai_provider", "gemini")
    with pytest.raises(AIProviderError):
        AIEngine()
