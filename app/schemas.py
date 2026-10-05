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
