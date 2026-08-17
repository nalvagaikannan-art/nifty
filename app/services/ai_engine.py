"""
AIEngine — AI reasons only; Rule-based engine decides first.
google-generativeai (பழைய library) — requirements.txt மாத்தாம
model மட்டும் gemini-2.5-flash-க்கு upgrade.

BUG FIX (2026-08-16):
  GEMINI_MODELS: "gemini-3.5-flash" / "gemini-3.6-flash" / "gemini-3.5-flash-lite"
  என்று இல்லாத model names இருந்தன — comment சரியா "gemini-2.5-flash" சொல்ல,
  code-ல் மட்டும் "3.x" versions-ஆக தவறா type ஆயிருந்தது.
  Fix: gemini-2.5-flash → gemini-2.5-flash-lite → gemini-2.0-flash
"""
import os, json, re, logging
from typing import Dict
import google.generativeai as genai
from openai import AsyncOpenAI
from pydantic import ValidationError
from app.config import settings
from app.exceptions import AIProviderError
from app.schemas import AIAnalysisResponse

logger = logging.getLogger(__name__)

GEMINI_ENABLED = os.environ.get("GEMINI_ENABLED", "false").lower() == "true"
_env_models = os.environ.get("GEMINI_MODELS", "")
# BUG FIX (2026-08-17): gemini-2.5-flash / gemini-2.5-pro-preview-06-05 now
# return 404 "no longer available to new users" (Google blocking these for
# recently-created API keys ahead of the Oct 16 2026 official shutdown —
# see log: "AI provider gemini failed: Gemini failed: 404 This model
# models/gemini-2.5-flash is no longer available to new users"). Current
# recommended lightweight/fast tier is the 3.x Flash family — falls back
# through 3.5-flash-lite -> 3.1-flash-lite -> 3.6-flash.
GEMINI_MODELS = [m.strip() for m in _env_models.split(",") if m.strip()] if _env_models else [
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash",
]
OPENAI_MODEL  = "gpt-4o-mini"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_URL   = "https://api.deepseek.com/v1"

VALID_BIAS = {"BULLISH", "BEARISH", "SIDEWAYS"}


class AIEngine:
    PRIORITY = ["gemini", "openai", "deepseek"]

    def __init__(self):
        self.keys = {
            "gemini":   settings.gemini_api_key,
            "openai":   settings.openai_api_key,
            "deepseek": settings.deepseek_api_key,
        }
        primary = settings.ai_provider.lower()
        order   = [primary] if self.keys.get(primary) else []
        order  += [p for p in self.PRIORITY if p not in order and self.keys.get(p)]
        if not GEMINI_ENABLED and "gemini" in order:
            order.remove("gemini")
        # BUG FIX (2026-08-17): previously raised AIProviderError right here
        # in __init__ when no key was configured. get_ai_engine() (deps.py)
        # constructs AIEngine() as a FastAPI Depends() — that resolves BEFORE
        # the route function body runs, so this raise happened outside every
        # route's try/except around `ai.analyze_market(...)`, producing an
        # "Unhandled AIProviderError" -> 503 instead of the intended
        # rule-engine-only fallback. Fix: store the empty order and defer the
        # error to analyze_market() (call time), which every caller already
        # wraps in try/except AIProviderError.
        self.order = order

    async def analyze_market(self, market_data: Dict) -> Dict:
        if not self.order:
            raise AIProviderError("No AI provider API key configured")
        decision = market_data.get("decision", {})
        prompt   = self._build_prompt(market_data, decision)

        errors = {}
        for provider in self.order:
            try:
                text   = await self._call(provider, prompt)
                parsed = self._parse(text)
                ai_agrees = parsed.get("market_bias", "") == decision.get("market_bias", "").upper()
                parsed["rule_market_bias"]    = decision.get("market_bias")
                parsed["bull_score"]          = decision.get("bull_score", 0)
                parsed["bear_score"]          = decision.get("bear_score", 0)
                parsed["rule_confidence"]     = decision.get("confidence", 0)
                parsed["ai_agrees"]           = ai_agrees
                parsed["_provider"]           = provider
                conf = decision.get("confidence", 50)
                conf = min(95, conf + 5) if ai_agrees else max(30, conf - 8)
                parsed["confidence"]          = conf
                parsed["market_bias"]         = decision.get("market_bias", parsed.get("market_bias", "Sideways"))
                parsed["bullish_probability"] = decision.get("bullish_probability", 50)
                parsed["bearish_probability"] = decision.get("bearish_probability", 50)
                parsed["preferred_side"]      = decision.get("preferred_side", "NONE")
                parsed["forecast"]            = decision.get("forecast", "Neutral")
                parsed["risk"]                = decision.get("risk", parsed.get("risk", "Medium"))
                parsed["disclaimer"]          = "Informational analysis only — not investment advice."

                # timeframe_trend: previously the AI was asked to invent a
                # 5min/15min/1hr reading from daily-close data alone (it had
                # no real intraday candles to look at). Now that real
                # intraday OHLC is fetched (see MarketAnalyzer._safe_multi_timeframe),
                # always prefer that ground truth over whatever the AI
                # guessed — same pattern as overriding market_bias/confidence
                # with the rule engine's numbers above. Frames neither broker
                # could fetch are marked "unavailable", not silently
                # backfilled with the AI's guess.
                multi_tf = market_data.get("multi_timeframe", {})
                if multi_tf:
                    parsed["timeframe_trend"] = {
                        label: multi_tf.get(label, {}).get("trend", "unavailable")
                        for label in ("5min", "15min", "1hr")
                    }
                _real_tf_sources = ("angel_one_intraday", "zerodha_intraday")
                _tf_source = next(
                    (multi_tf.get(l, {}).get("data_source") for l in ("5min", "15min", "1hr")
                     if multi_tf.get(l, {}).get("data_source") in _real_tf_sources),
                    None,
                )
                parsed["timeframe_data_source"] = _tf_source or "unavailable"

                try:
                    AIAnalysisResponse(
                        market_bias=parsed["market_bias"],
                        bullish_probability=parsed["bullish_probability"],
                        bearish_probability=parsed["bearish_probability"],
                        preferred_side=parsed["preferred_side"],
                        support=parsed.get("support", []),
                        resistance=parsed.get("resistance", []),
                        pcr=parsed.get("pcr", 0),
                        vix=parsed.get("vix", 0),
                        reason=parsed.get("reason", ""),
                        risk=parsed["risk"],
                        confidence=parsed["confidence"],
                    )
                except ValidationError as ve:
                    logger.warning(f"Schema validation warning: {ve}")

                logger.info(f"AI provider {provider} succeeded")
                return parsed
            except Exception as e:
                logger.warning(f"AI provider {provider} failed: {e}")
                errors[provider] = str(e)

        raise AIProviderError(f"All AI providers failed: {errors}")

    async def _call(self, provider: str, prompt: str) -> str:
        if provider == "gemini":
            return await self._gemini(prompt)
        elif provider == "openai":
            return await self._openai(prompt)
        elif provider == "deepseek":
            return await self._deepseek(prompt)
        raise AIProviderError(f"Unknown provider: {provider}")

    async def _gemini(self, prompt: str) -> str:
        """
        google-generativeai library use பண்றோம் (requirements.txt மாறாது).
        Model: gemini-2.5-flash (1.5/2.0 shutdown ஆயிடுச்சு).
        """
        genai.configure(api_key=self.keys["gemini"])
        last = None
        for m in GEMINI_MODELS:
            try:
                model = genai.GenerativeModel(m)
                r = await model.generate_content_async(prompt)
                text = r.text
                if text:
                    logger.debug(f"Gemini model {m} succeeded")
                    return text
                raise AIProviderError(f"Empty response from {m}")
            except Exception as e:
                logger.debug(f"Gemini model {m} failed: {e}")
                last = e
        raise AIProviderError(f"Gemini failed: {last}")

    async def _openai(self, prompt: str) -> str:
        c = AsyncOpenAI(api_key=self.keys["openai"])
        r = await c.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return r.choices[0].message.content

    async def _deepseek(self, prompt: str) -> str:
        c = AsyncOpenAI(api_key=self.keys["deepseek"], base_url=DEEPSEEK_URL)
        r = await c.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return r.choices[0].message.content

    def _build_prompt(self, md: Dict, decision: Dict) -> str:
        spot   = md.get("spot", {})
        pcr    = md.get("pcr", 0)
        vix    = md.get("vix", 0)
        sr     = md.get("support_resistance", {})
        tech   = md.get("technicals", {})
        oi_sum = md.get("oi_summary", {})
        rule_reasons = "\n".join(decision.get("reasons", [])[:10])

        multi_tf = md.get("multi_timeframe", {}) or {}
        _real_tf_sources = ("angel_one_intraday", "zerodha_intraday")

        def _tf_line(label: str) -> str:
            frame = multi_tf.get(label, {})
            if frame.get("data_source") not in _real_tf_sources:
                return f"- {label}: data not available (no broker connected — do NOT guess a value for this)"
            ind = frame.get("indicators", {})
            return (f"- {label}: trend={frame.get('trend','?')}, "
                    f"close={frame.get('last_close',0):,.0f}, ADX={ind.get('adx',0):.1f}, "
                    f"+DI={ind.get('di_plus',0):.1f}, -DI={ind.get('di_minus',0):.1f}, "
                    f"VWAP={ind.get('vwap',0):,.0f}")

        tf_block = "\n".join(_tf_line(l) for l in ("5min", "15min", "1hr"))
        have_real_tf = any(multi_tf.get(l, {}).get("data_source") in _real_tf_sources for l in ("5min", "15min", "1hr"))

        return f"""You are an expert NSE options analyst writing an ANALYSIS ONLY report — never a trade instruction. A rule-based engine already scored {md.get('symbol','NIFTY')} and gave:

RULE ENGINE RESULT:
- Market Bias: {decision.get('market_bias','Sideways')} (Bullish {decision.get('bullish_probability',50)}% / Bearish {decision.get('bearish_probability',50)}%)
- Preferred Side: {decision.get('preferred_side','NONE')}
- Bull Score: {decision.get('bull_score',0)} | Bear Score: {decision.get('bear_score',0)}
- Confidence: {decision.get('confidence',50)}%
- Forecast: {decision.get('forecast','Neutral')}
- Risk: {decision.get('risk','Medium')}

TOP RULE SIGNALS:
{rule_reasons}

MARKET DATA (DAILY, from historical closes):
- Spot: {spot.get('price',0):,.0f} | Change: {spot.get('change_percent',0):.2f}%
- PCR: {pcr:.3f} | Max Pain: {md.get('max_pain',0):,.0f}
- India VIX: {vix:.2f} (volatility measure — NOT directional, do not use it to call a side)
- RSI: {md.get('rsi',50):.1f} | MACD: {md.get('macd',{}).get('macd',0):.1f}
- EMA20: {tech.get('ema20',0):,.0f} | EMA50: {tech.get('ema50',0):,.0f}
- ADX: {tech.get('adx',0):.1f} | Supertrend: {tech.get('supertrend','N/A')}
- ATM IV: {oi_sum.get('atm_iv',0):.1f}%
- CE Max OI Strike: {oi_sum.get('ce_max_oi_strike',0):,.0f}
- PE Max OI Strike: {oi_sum.get('pe_max_oi_strike',0):,.0f}
- Support: {sr.get('support',[])} | Resistance: {sr.get('resistance',[])}
- Market Open: {spot.get('market_open',False)}

REAL INTRADAY MULTI-TIMEFRAME DATA (actual candles, not derived from daily close):
{tf_block}

YOUR TASK (JSON only, no markdown):
1. Agree or disagree with rule engine's market bias based on data
2. EXPLAIN in Tamil+English mix — describe what data shows, NOT a trade instruction
3. timeframe_trend: {"report the ACTUAL trend shown in the REAL INTRADAY MULTI-TIMEFRAME DATA above for each frame that has data — do not invent numbers for it" if have_real_tf else 'ALL THREE intraday timeframes are marked "data not available" above — you MUST output "unavailable" for all three in timeframe_trend below. Do NOT guess or infer 5min/15min/1hr values from the daily data; that would be fabricated.'}

Output EXACTLY this JSON:
{{
  "market_bias": "<BULLISH|BEARISH|SIDEWAYS>",
  "market_trend": "<bullish|bearish|sideways>",
  "reason": "<80-120 words explaining WHY market looks this way>",
  "key_factors": ["factor1","factor2","factor3","factor4","factor5"],
  "timeframe_trend": {{"5min":"<up|down|sideways|unavailable>","15min":"<up|down|sideways|unavailable>","1hr":"<up|down|sideways|unavailable>"}},
  "support": [<float>,<float>],
  "resistance": [<float>,<float>],
  "risk": "<Low|Medium|High>",
  "vix": {vix:.2f},
  "pcr": {pcr:.3f}
}}"""

    def _parse(self, text: str) -> Dict:
        if not text:
            raise AIProviderError("Empty AI response")
        clean = re.sub(r"```(?:json)?", "", text).strip()
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start < 0 or end <= start:
            raise AIProviderError("No JSON in AI response")
        data = json.loads(clean[start:end])
        data["market_bias"] = data.get("market_bias", "SIDEWAYS").upper().strip()
        if data["market_bias"] not in VALID_BIAS:
            data["market_bias"] = "SIDEWAYS"
        data["market_trend"] = data.get("market_trend", "sideways").lower()
        data["risk"] = data.get("risk", "Medium").strip().title()
        return data