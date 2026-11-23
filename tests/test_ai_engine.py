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


@pytest.mark.parametrize("mode", ["low_oi", "low_volume", "legacy"])
def test_build_prompt_chain_quality_rules(mode):
    engine = AIEngine()
    md = {
        "symbol": "NIFTY", "spot": {"price": 24000},
        "pcr": 1.45, "max_pain": 24000,
        "option_chain_valid": True, "option_chain_rows": 42,
        "oi_summary": {
            "call_writing_confirmed": True, "put_writing_confirmed": False,
            "ce_max_oi_strike": 24200, "pe_max_oi_strike": 23800,
        },
    }
    q = {
        "available": True, "status": "limited",
        "oi_usable": mode != "low_oi", "oi_coverage_pct": 32.0 if mode == "low_oi" else 95.0,
        "volume_usable": mode != "low_volume", "volume_coverage_pct": 20.0 if mode == "low_volume" else 90.0,
        "reason": "OI coverage low", "volume_reason": "Volume coverage low",
    }
    if mode == "low_oi":
        md["option_chain_quality"] = q
    elif mode == "low_volume":
        md["pcr_quality"] = {"chain_quality": q}

    prompt = engine._build_prompt(md, {"preferred_side": "NONE"})
    low = prompt.lower()

    if mode == "low_oi":
        assert "pcr: unavailable" in low and "max pain: unavailable" in low
        assert '"pcr": 0.000' in prompt
        assert "confirmed call writing: unavailable" in low
        assert "ce max oi strike: unavailable" in low
        assert "do not use pcr, max pain, oi changes" in low
    elif mode == "low_volume":
        assert "pcr: 1.450" in low and "max pain: 24,000" in low
        assert "confirmed call writing: unavailable" in low
        assert '"pcr": 1.450' in prompt
        assert "do not claim confirmed call/put writing" in low
    else:
        assert "status=not reported" in low
        assert "pcr: 1.450" in low and "max pain: 24,000" in low
        assert "confirmed call writing: true" in low
        assert '"pcr": 1.450' in prompt


def test_prompt_requires_tamil_script_not_tanglish():
    engine = AIEngine()
    prompt = engine._build_prompt(
        {"symbol": "NIFTY", "spot": {"price": 24000}},
        {"preferred_side": "NONE"},
    )
    assert "using Tamil script" in prompt
    assert "Do NOT write Tanglish" in prompt
