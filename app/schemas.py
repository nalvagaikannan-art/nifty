from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Literal
from datetime import datetime

class MarketSnapshot(BaseModel):
    symbol: str
    price: float
    change: float
    change_percent: float
    high: float
    low: float
    volume: int
    timestamp: datetime

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
