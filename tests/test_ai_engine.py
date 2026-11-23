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


def test_readiness_status_never_exposes_provider_secrets(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-secret")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "ai_provider", "openai")

    status = AIEngine().readiness_status()

    assert status["ready"] is True
    assert status["effective_provider_order"] == ["openai"]
    assert status["configured"] == {
        "gemini": False, "openai": True, "deepseek": False,
    }
    assert "test-openai-secret" not in str(status)
    assert status["mode"] == "ai_validator_and_explainer"


def test_gemini_key_alone_is_not_ready_when_gemini_disabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-secret")
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "ai_provider", "gemini")
    monkeypatch.setattr("app.services.ai_engine.GEMINI_ENABLED", False)

    status = AIEngine().readiness_status()

    assert status["configured"]["gemini"] is True
    assert status["gemini_enabled"] is False
    assert status["ready"] is False
    assert status["effective_provider_order"] == []


@pytest.mark.asyncio
async def test_provider_runtime_telemetry_records_success_without_payload(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", "secret-not-for-logs")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    engine = AIEngine()
    recorded = []

    async def fake_call(provider, prompt):
        assert provider == "openai"
        return "provider-response"

    async def fake_record(provider, outcome, latency_ms, error_category=None, attempt_number=1):
        recorded.append((provider, outcome, error_category, attempt_number))

    monkeypatch.setattr(engine, "_call", fake_call)
    monkeypatch.setattr(engine, "_parse", lambda raw: {
        "market_bias": "SIDEWAYS", "risk": "Medium",
        "bullish_probability": 50, "bearish_probability": 50,
        "preferred_side": "NONE", "reason": "test",
    })
    monkeypatch.setattr(engine, "_record_provider_event", fake_record)

    result = await engine.analyze_market({"decision": {"market_bias": "SIDEWAYS"}})

    assert result["_provider"] == "openai"
    assert recorded == [("openai", "success", None, 1)]
    assert "secret-not-for-logs" not in str(recorded)


@pytest.mark.asyncio
async def test_provider_runtime_telemetry_records_fallback_and_sanitized_failure(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", "openai-secret")
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-secret")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    engine = AIEngine()
    recorded = []

    async def fake_call(provider, prompt):
        if provider == "openai":
            raise RuntimeError("request failed with api_key=openai-secret")
        return "provider-response"

    async def fake_record(provider, outcome, latency_ms, error_category=None, attempt_number=1):
        recorded.append((provider, outcome, error_category, attempt_number))

    monkeypatch.setattr(engine, "_call", fake_call)
    monkeypatch.setattr(engine, "_parse", lambda raw: {
        "market_bias": "SIDEWAYS", "risk": "Medium",
        "bullish_probability": 50, "bearish_probability": 50,
        "preferred_side": "NONE", "reason": "test",
    })
    monkeypatch.setattr(engine, "_record_provider_event", fake_record)

    result = await engine.analyze_market({"decision": {"market_bias": "SIDEWAYS"}})

    assert result["_provider"] == "deepseek"
    assert recorded[0] == ("openai", "failure", "provider_error", 1)
    assert recorded[1] == ("deepseek", "success", None, 2)
    assert "openai-secret" not in str(recorded)


@pytest.mark.asyncio
async def test_all_provider_failures_do_not_expose_raw_errors(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", "openai-secret")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    engine = AIEngine()

    async def fake_call(provider, prompt):
        raise RuntimeError("api_key=openai-secret")

    async def ignore_record(*args, **kwargs):
        return None

    monkeypatch.setattr(engine, "_call", fake_call)
    monkeypatch.setattr(engine, "_record_provider_event", ignore_record)

    with pytest.raises(AIProviderError) as exc:
        await engine.analyze_market({"decision": {"market_bias": "SIDEWAYS"}})

    assert "openai-secret" not in str(exc.value)
    assert "api_key" not in str(exc.value)
