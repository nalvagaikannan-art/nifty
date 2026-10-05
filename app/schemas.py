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


class StrategyRecommendationResponse(BaseModel):
    """
    Public API contract for /api/strategy/recommend/{symbol}.

    Strategy output contains several nested engine payloads whose internal
    structures may evolve independently, so those objects remain flexible.
    Extra fields are preserved for backward compatibility with branch-specific
    fields such as signal_lifecycle_reason.
    """
    action: str
    ai_reason: str
    all_expiries: List[str]
    bear_score: int
    best_score: float
    best_strategy: str
    bull_score: int
    candidates: Dict
    confidence: int
    confluence: Dict
    data_completeness_pct: int
    disclaimer: str
    entry_gate: Dict
    expected_move: Dict
    expiry: str
    expiry_info: Dict
    hard_gated: bool
    iv_info: Dict
    lot_size: int
    macd: Dict
    margin: int
    market_bias: str
    market_open: bool
    market_regime: Dict
    market_regime_confidence: str
    market_regime_no_trade: bool
    market_regime_no_trade_reason: str
    market_regime_reasons: List[str]
    market_snapshot_timestamp: Optional[str] = None
    market_state: str
    max_pain: float
    multi_timeframe: Dict
    pcr: float
    preferred_side: str
    price_levels: Optional[Dict] = None
    raw_preferred_side: str
    reversal_type: str
    risk: str
    rsi: float
    signal_action: str
    signal_active_side: str
    signal_candidate: str
    signal_confirmations: int
    signal_history: List[Dict]
    signal_lifecycle: str
    signal_reversal: bool
    signal_reversal_confirmations: int
    signal_strength: int
    spot: float
    strategy: str
    strategy_detail: Optional[Dict] = None
    strategy_reason: str
    strikes: List[Dict]
    symbol: str
    technical_data_source: str
    technicals: Dict
    technicals_daily: Dict
    time_session: str
    trade_confidence: int
    v2_risk: Optional[Dict] = None
    v2_trade_levels: Optional[Dict] = None
    vix: float
    volatility_label: str
    volatility_regime: str
    wait_reasons: List[str]
    warnings: List[str]
    win_probability: Optional[float] = None
    win_probability_note: str

    model_config = {"extra": "allow"}


class OptionsChainAnalyticsResponse(BaseModel):
    """
    Public API contract for /api/options/chain/{symbol}.

    The option-chain rows and derived analytics contain provider-specific
    nested fields, so nested payloads remain flexible while the top-level
    API contract is explicit.
    """
    symbol: str
    expiry: str
    all_expiries: List[str]
    underlying_price: float
    pcr: float
    max_pain: float
    oi_summary: Dict
    candidates: Dict
    data_source: str
    data: List[Dict]

    model_config = {"extra": "allow"}


class OptionsChainAliasResponse(OptionsChainAnalyticsResponse):
    """
    Public API contract for /api/options/{symbol}.

    This alias exposes three ATM/IV fields flattened from oi_summary.
    """
    atm_call_oi: Optional[float] = None
    atm_put_oi: Optional[float] = None
    iv_skew: Optional[float] = None

    model_config = {"extra": "allow"}


class DashboardStatusResponse(BaseModel):
    ai_provider_configured: bool
    ai_providers_available: List[str]
    angel_one_connected: bool
    market_open: bool
    model_config = {"extra": "allow"}


class DashboardSummaryItem(BaseModel):
    price: Optional[float] = None
    change: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    pcr: Optional[float] = None
    vix: Optional[float] = None
    max_pain: Optional[float] = None
    market_open: Optional[bool] = None
    market_bias: Optional[str] = None
    bullish_probability: Optional[int] = None
    bearish_probability: Optional[int] = None
    preferred_side: Optional[str] = None
    bull_score: Optional[int] = None
    bear_score: Optional[int] = None
    confidence: Optional[int] = None
    forecast: Optional[str] = None
    risk: Optional[str] = None
    error: Optional[str] = None
    model_config = {"extra": "allow"}


class DashboardSummaryResponse(BaseModel):
    model_config = {"extra": "allow"}

    NIFTY: Optional[DashboardSummaryItem] = None
    BANKNIFTY: Optional[DashboardSummaryItem] = None
    FINNIFTY: Optional[DashboardSummaryItem] = None
    SENSEX: Optional[DashboardSummaryItem] = None


class MarketCandlesResponse(BaseModel):
    available: bool
    bar_count: int = 0
    closes: Optional[List[float]] = None
    data_source: Optional[str] = None
    fresh: Optional[bool] = None
    freshness_minutes: Optional[float] = None
    highs: Optional[List[float]] = None
    interval: Optional[str] = None
    latest_timestamp: Optional[str] = None
    lows: Optional[List[float]] = None
    market_open: Optional[bool] = None
    opens: Optional[List[float]] = None
    session_date: Optional[str] = None
    timestamps: Optional[List[str]] = None
    volumes: Optional[List[float]] = None
    reason: Optional[str] = None
    model_config = {"extra": "allow"}


class MarketVixResponse(BaseModel):
    vix: float
    model_config = {"extra": "allow"}


class MarketBreadthResponse(BaseModel):
    advances: int
    declines: int
    unchanged: int
    source: str
    model_config = {"extra": "allow"}


class AccuracyIndicatorResponse(BaseModel):
    id: str
    icon: str
    title_ta: str
    hits: int
    total: int
    success_rate: float
    insufficient_data: bool
    metric_type: str
    model_config = {"extra": "allow"}


class AccuracyIndicatorsOverallResponse(BaseModel):
    hits: int
    total: int
    success_rate: float
    insufficient_data: bool
    model_config = {"extra": "allow"}


class AccuracyIndicatorsResponse(BaseModel):
    days: int
    horizon_minutes: int
    indicators: List[AccuracyIndicatorResponse]
    overall: AccuracyIndicatorsOverallResponse
    snapshots_used: int
    symbol: str
    model_config = {"extra": "allow"}


class AccuracySignalsResponse(BaseModel):
    accuracy_basis: str
    actionable_episodes: int
    by_confidence_range: Dict
    by_horizon: Dict
    by_regime: Dict
    call_buy_accuracy: Dict
    call_sell_accuracy: Dict
    days: int
    episode_gap_minutes: int
    headline_horizon_minutes: int
    no_directional_recommendation: int
    no_price_data: int
    overall: Dict
    pending_at_headline_horizon: int
    put_buy_accuracy: Dict
    put_sell_accuracy: Dict
    signals_seen: int
    symbol: str
    model_config = {"extra": "allow"}


class AccuracyPremiumResponse(BaseModel):
    accuracy_basis: str
    actionable_episodes: int
    by_horizon: Dict
    by_moneyness: Dict
    by_strike: List[Dict]
    call_buy_accuracy: Dict
    call_buy_accuracy_net_of_costs: Dict
    call_sell_accuracy: Dict
    call_sell_accuracy_net_of_costs: Dict
    days: int
    disagreement: Dict
    episode_gap_minutes: int
    estimated_round_trip_cost_pct: float
    headline_horizon_minutes: int
    mfe_mae: Dict
    no_premium_data: int
    overall: Dict
    overall_net_of_costs: Dict
    pending_at_headline_horizon: int
    put_buy_accuracy: Dict
    put_buy_accuracy_net_of_costs: Dict
    put_sell_accuracy: Dict
    put_sell_accuracy_net_of_costs: Dict
    signals_seen: int
    signals_with_recommendation: int
    signals_without_recommendation: int
    symbol: str
    model_config = {"extra": "allow"}


class ConfidenceCalibrationResponse(BaseModel):
    actionable_episodes_total: int
    calibration_basis: str
    calibration_curve: List[Dict]
    confidence_bucket: Optional[str] = None
    disclaimer: str
    episode_gap_minutes: int
    episodes: int
    graded: int
    historical_win_rate_pct: Optional[float] = None
    horizon_minutes: int
    insufficient_data: bool
    lookback_days: int
    min_signals_required: int
    pending_episodes: int
    sample_size: int
    signal_strength: float
    symbol: str
    unknown_confidence_bucket_episodes: int
    model_config = {"extra": "allow"}


class AccuracyStatusSavedResponse(BaseModel):
    market_snapshots: int
    ai_signals: int
    option_rows: int
    model_config = {"extra": "allow"}


class AccuracyStatusTimestampsResponse(BaseModel):
    market: Optional[str] = None
    analysis: Optional[str] = None
    model_config = {"extra": "allow"}


class AccuracyStatusResponse(BaseModel):
    symbol: str
    saved: AccuracyStatusSavedResponse
    last_saved_utc: AccuracyStatusTimestampsResponse
    database_configured: bool
    note: str
    recent_errors: List[Dict]
    model_config = {"extra": "allow"}


class AngelStatusResponse(BaseModel):
    client_id: str
    configured: bool
    logged_in: bool
    session_age_minutes: float
    model_config = {"extra": "allow"}


class AngelLiveFeedResponse(BaseModel):
    websocket: Dict
    ticks: Dict
    model_config = {"extra": "allow"}


class AngelLtpResponse(BaseModel):
    change: float
    change_percent: float
    close: float
    high: float
    low: float
    open: float
    price: float
    source: str
    symbol: str
    timestamp: str
    model_config = {"extra": "allow"}


class AngelCandleRowResponse(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    model_config = {"extra": "allow"}


class AngelCandlesResponse(BaseModel):
    symbol: str
    interval: str
    candles: List[AngelCandleRowResponse]
    model_config = {"extra": "allow"}


class AngelOptionChainResponse(BaseModel):
    symbol: str
    expiry: Optional[str] = None
    data: Dict
    model_config = {"extra": "allow"}


class OptionPcrResponse(BaseModel):
    symbol: str
    pcr: float
    model_config = {"extra": "allow"}


class OptionMaxPainResponse(BaseModel):
    symbol: str
    max_pain: float
    model_config = {"extra": "allow"}


class FiiDiiResponse(BaseModel):
    date: str
    dii: Dict
    fii: Dict
    source: str
    model_config = {"extra": "allow"}


class SectorPerformanceResponse(BaseModel):
    advancing: int
    declining: int
    rotation: str
    sectors: List[Dict]
    source: str
    top_sector: Dict
    weak_sector: Dict
    model_config = {"extra": "allow"}


class GlobalMarketsResponse(BaseModel):
    gift_nifty_change_pct: float
    gift_nifty_expiry: str
    gift_nifty_price: float
    gift_nifty_status: str
    global_change_pct: float
    instruments: Dict
    source: str
    model_config = {"extra": "allow"}


class EconomicCalendarResponse(BaseModel):
    expiry: Dict
    macro_events: List[Dict]
    macro_source: str
    model_config = {"extra": "allow"}


class PaperTradeOpenResponse(BaseModel):
    open_trades: List[Dict]
    count: int
    model_config = {"extra": "allow"}


class PaperTradeHistoryResponse(BaseModel):
    history: List[Dict]
    count: int
    model_config = {"extra": "allow"}


class PaperTradeStatsResponse(BaseModel):
    avg_r: Optional[float] = None
    by_regime: Dict
    by_strength: Dict
    calibration_note: str
    daily_pnl: float
    losses: int
    open_trades: int
    total_pnl: float
    total_trades: int
    win_rate: Optional[float] = None
    wins: int
    model_config = {"extra": "allow"}


class DailyPnlResponse(BaseModel):
    daily_pnl: float
    model_config = {"extra": "allow"}


class PositionItemResponse(BaseModel):
    symbol: str
    quantity: int
    avg_price: float
    ltp: float
    side: str
    pnl: float
    status: str
    ai_suggestion: str
    ai_reasons: List[str]
    change_pct: float
    days_to_expiry: Optional[int] = None
    vix: Optional[float] = None
    pcr: Optional[float] = None
    market_bias: Optional[str] = None
    stop_loss_hit: bool
    market_data_age_seconds: Optional[float] = None
    model_config = {"extra": "allow"}


class PositionsResponse(BaseModel):
    positions: List[PositionItemResponse]
    total_pnl: float
    count: int
    disclaimer: str
    model_config = {"extra": "allow"}


class SettingsConfigResponse(BaseModel):
    ai_provider: str
    cache_ttl: int
    log_level: str
    angel_configured: bool
    angel_client_id: Optional[str] = None
    zerodha_configured: bool
    model_config = {"extra": "allow"}


class SettingsHealthResponse(BaseModel):
    sources: Dict
    alert_webhook_configured: bool
    redis_configured: bool
    database: str
    model_config = {"extra": "allow"}


class StrategyHistoryItemResponse(BaseModel):
    symbol: str
    strategy: str
    score: float
    market_state: str
    confidence: float
    spot: float
    pcr: float
    vix: float
    reversal: bool
    reversal_type: str
    timestamp: str
    date: str
    reasons: List[str]
    model_config = {"extra": "allow"}


class StrategyHistoryResponse(BaseModel):
    symbol: str
    history: List[StrategyHistoryItemResponse]
    count: int
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
