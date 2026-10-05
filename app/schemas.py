from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Literal
from datetime import datetime

class MarketSpotResponse(BaseModel):
    """
    Public API contract for /api/market/spot/{symbol}.

    Covers the common 13-field REST/NSE/Zerodha shape and the additional
    Angel One WebSocket freshness metadata when available.
    Extra provider fields are preserved so the API does not silently lose
    upstream data during response-model validation.
    """
    symbol: str
    price: float
    change: float
    change_percent: float
    high: float
    low: float
    open: float
    prev_close: float
    volume: int
    market_open: bool
    market_status_source: str
    data_source: str
    timestamp: datetime
    exchange_timestamp: Optional[float] = None
    tick_age_sec: Optional[float] = None

    model_config = {"extra": "allow"}


class MarketSnapshot(BaseModel):
    symbol: str
    price: float
    change: float
    change_percent: float
    high: float
    low: float
    volume: int
    timestamp: datetime

class MarketOptionChainItemResponse(BaseModel):
    """
    Public API row contract for /api/market/option-chain/{symbol}.
    CE/PE retain the provider-specific option-leg payload unchanged.
    """
    strikePrice: float
    expiryDate: str
    CE: Optional[Dict] = None
    PE: Optional[Dict] = None

    model_config = {"extra": "allow"}


class MarketOptionChainResponse(BaseModel):
    """
    Public API contract for /api/market/option-chain/{symbol}.

    Supports Angel One, Zerodha and NSE fallback paths. Broker-specific
    metadata such as lot_size/live websocket metadata is optional because
    those fields are not guaranteed on every fallback provider.
    """
    symbol: str
    expiry: str
    all_expiries: List[str]
    underlying_price: float
    data: List[MarketOptionChainItemResponse]
    data_source: Optional[str] = None
    lot_size: Optional[int] = None
    live_option_data_source: Optional[str] = None
    live_option_ticks_merged: Optional[int] = None

    model_config = {"extra": "allow"}


class OptionChainItem(BaseModel):
    strike: float
    ce: Optional[Dict] = None
    pe: Optional[Dict] = None

class OptionChainResponse(BaseModel):
    symbol: str
    expiry: str
    underlying_price: float
    options: List[OptionChainItem]

class AnalysisDecisionResponse(BaseModel):
    """
    Public API contract for /api/analysis/decision/{symbol}.

    Nested observation/strategy objects remain flexible because their
    internal shapes can evolve independently of this route contract.
    Extra fields are preserved for backward compatibility.
    """
    symbol: str
    market_bias: str
    bullish_probability: int
    bearish_probability: int
    preferred_side: str
    signal_lifecycle: str
    signal_candidate: str
    signal_confirmations: int
    signal_reversal_confirmations: int
    signal_active_side: str
    recommended_strike: str
    bull_score: int
    bear_score: int
    confidence: int
    signal_strength: int
    forecast: str
    volatility_regime: str
    volatility_label: str
    risk: str
    reasons: List[str]
    strategy: str
    strategy_reason: str
    strategy_detail: Optional[Dict] = None
    price_levels: Optional[Dict] = None
    scenarios: List[Dict]
    pcr: float
    max_pain: float
    vix: float
    rsi: float
    macd: Dict
    technicals: Dict
    multi_timeframe: Dict
    session_state: Dict
    oi_change_tracked: Dict
    support_resistance: Dict
    tamil_indicators: List[Dict]
    option_volume: Dict
    market_open: bool
    expiry_risk: Dict
    data_quality: Dict
    disclaimer: str

    model_config = {"extra": "allow"}



class AIAnalysisRouteResponse(BaseModel):
    """
    Public API contract for /api/analysis/ai/{symbol}.

    This is intentionally separate from AIAnalysisResponse, which is used
    internally by AIEngine for validating the model's core AI fields.
    Nested analysis objects remain flexible because their internal payloads
    evolve independently of this route contract.
    """
    _provider: str
    ai_agrees: Optional[bool] = None
    all_expiries: List[str]
    all_reasons: List[str]
    bear_score: int
    bearish_probability: int
    best_strategy: str
    bull_score: int
    bullish_probability: int
    candidates: Dict
    confidence: int
    confidence_calibration: Dict
    confluence: Dict
    data_completeness_pct: int
    data_quality: Dict
    disclaimer: str
    expiry: str
    expiry_risk: Dict
    forecast: str
    futures_premium_pct_value: float
    futures_premium_value: float
    hard_gated: bool
    key_factors: List[str]
    margin: int
    market_bias: str
    market_open: bool
    market_regime: str
    market_regime_confidence: str
    market_regime_reasons: List[str]
    market_snapshot_timestamp: Optional[str] = None
    market_trend: str
    max_pain: float
    multi_timeframe: Dict
    oi_change_tracked: Dict
    oi_summary: Dict
    option_volume: Dict
    pcr: float
    preferred_side: str
    price_levels: Optional[Dict] = None
    reason: str
    recommended_option: Optional[Dict] = None
    recommended_strike: str
    resistance: List[float]
    risk: str
    rule_confidence: int
    rule_market_bias: str
    scenarios: List[Dict]
    session_context: Dict
    session_state: Dict
    signal_action: str
    signal_active_side: str
    signal_candidate: str
    signal_confirmations: int
    signal_lifecycle: str
    signal_reversal_confirmations: int
    signal_strength: int
    spot: Dict
    strategy: str
    strategy_detail: Optional[Dict] = None
    strategy_reason: str
    support: List[float]
    support_resistance: Dict
    symbol: str
    tamil_indicators: List[Dict]
    technical_data_source: str
    timeframe_data_source: str
    timeframe_trend: Dict
    v3_gate: Dict
    vix: float
    volatility_label: str
    volatility_regime: str

    model_config = {"extra": "allow"}

class TechnicalIndicators(BaseModel):
    support: List[float]
    resistance: List[float]
    trend: str  # bullish, bearish, sideways
    rsi: float
    macd: Dict
    volume_spike: bool
    breakout: bool
    breakdown: bool

class AIAnalysisResponse(BaseModel):
    """
    Analysis-only response — a market-direction read and its confidence,
    never a buy/sell instruction. `preferred_side` says which option side
    the read currently favours (CALL/PUT/NONE); it is not an order.

    VALIDATION FIX (carried over from the original version of this file):
    this schema exists so a malformed AI field (e.g. confidence: 150, or a
    bias value outside the allowed set) is caught before reaching the
    frontend — see AIEngine.analyze_market(), which now validates every
    response against this schema.
    """
    market_bias: Literal["Bullish", "Bearish", "Sideways"]
    bullish_probability: int = Field(ge=0, le=100)
    bearish_probability: int = Field(ge=0, le=100)
    preferred_side: Literal["CALL", "PUT", "NONE"]
    support: List[float]
    resistance: List[float]
    pcr: float
    vix: float
    reason: str
    risk: Literal["Low", "Medium", "High"]
    confidence: int = Field(ge=0, le=100)
    disclaimer: str = "Informational analysis only — not investment advice."

    @field_validator("market_bias", mode="before")
    @classmethod
    def _titlecase_bias(cls, v):
        return v.strip().title() if isinstance(v, str) else v

    @field_validator("preferred_side", mode="before")
    @classmethod
    def _uppercase_side(cls, v):
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("risk", mode="before")
    @classmethod
    def _titlecase_risk(cls, v):
        return v.strip().title() if isinstance(v, str) else v
