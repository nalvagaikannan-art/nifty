from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Text, UniqueConstraint, Index, ForeignKey
from sqlalchemy.sql import func
from app.database import Base

class MarketData(Base):
    __tablename__ = "market_data"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    price = Column(Float)
    timestamp = Column(DateTime, server_default=func.now())

class OptionData(Base):
    __tablename__ = "option_data"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)  # NIFTY, BANKNIFTY, FINNIFTY
    expiry = Column(String)
    strike = Column(Float)
    option_type = Column(String)  # CE/PE
    last_price = Column(Float)
    change = Column(Float)
    volume = Column(Integer)
    open_interest = Column(Integer)
    implied_volatility = Column(Float)
    timestamp = Column(DateTime, server_default=func.now())

class AnalysisResult(Base):
    __tablename__ = "analysis_results"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String)
    analysis_type = Column(String)  # 'ai', 'technical', 'risk'
    result = Column(JSON)
    timestamp = Column(DateTime, server_default=func.now())

class SignalState(Base):
    __tablename__ = "signal_states"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True, nullable=False)
    active_side = Column(String, nullable=False, default="NONE")
    candidate_side = Column(String, nullable=False, default="NONE")
    confirmations = Column(Integer, nullable=False, default=0)
    reversal_confirmations = Column(Integer, nullable=False, default=0)
    lifecycle = Column(String, nullable=False, default="WAIT")
    last_confirmation_at = Column(DateTime, nullable=True)
    last_evaluation_at = Column(DateTime, nullable=True)
    strategy = Column(String, nullable=False, default="WAIT")
    strategy_score = Column(Float, nullable=False, default=0)
    margin = Column(Float, nullable=False, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SignalHistory(Base):
    __tablename__ = "signal_history"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True, nullable=False)
    strategy = Column(String, nullable=False)
    score = Column(Float, nullable=False, default=0)
    market_state = Column(String, nullable=False, default="UNKNOWN")
    confidence = Column(Float, nullable=False, default=0)
    spot = Column(Float, nullable=False, default=0)
    pcr = Column(Float, nullable=False, default=0)
    vix = Column(Float, nullable=False, default=0)
    reversal = Column(Integer, nullable=False, default=0)
    reversal_type = Column(String, nullable=False, default="")
    reasons = Column(JSON, nullable=True)
    timestamp = Column(DateTime, server_default=func.now(), index=True)




class DailySignalLedger(Base):
    """
    Permanent audit ledger for actionable daily strategy signals.

    This is intentionally separate from SignalHistory, which is only a
    rolling UI history. Outcome is collected later; P0 only records the
    entry-time evidence snapshot.
    """
    __tablename__ = "daily_signal_ledger"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    symbol = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)
    option_type = Column(String, nullable=False)
    strike = Column(Float, nullable=False)
    expiry = Column(String, nullable=True)

    spot = Column(Float, nullable=False, default=0)
    option_ltp_snapshot = Column(Float, nullable=True)
    entry_price = Column(Float, nullable=True)

    signal_strength = Column(Float, nullable=False, default=0)
    confidence = Column(Float, nullable=False, default=0)
    lifecycle = Column(String, nullable=False, default="UNKNOWN")
    confirmations = Column(Integer, nullable=False, default=0)

    market_snapshot_timestamp = Column(String, nullable=True)
    technical_data_source = Column(String, nullable=True)

    confluence = Column(JSON, nullable=True)
    mtf_freshness = Column(JSON, nullable=True)
    entry_snapshot = Column(JSON, nullable=True)

    # P0: NULL until a later outcome/replay phase is explicitly implemented.
    outcome = Column(String, nullable=True)

    # Stable duplicate key for repeated browser/API observations.
    ledger_key = Column(String, nullable=False, unique=True, index=True)

    __table_args__ = (
        Index(
            "ix_daily_signal_ledger_symbol_timestamp",
            "symbol",
            "timestamp",
        ),
        Index(
            "ix_daily_signal_ledger_symbol_action_timestamp",
            "symbol",
            "action",
            "timestamp",
        ),
    )


class IntradayOHLC(Base):
    """
    Persisted real completed 5-minute intraday candles.

    15-minute and 1-hour frames are derived locally from these raw 5-minute
    candles, so there is only one source-of-truth candle stream to persist.
    """
    __tablename__ = "intraday_ohlc"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True, nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)

    # `open` is nullable because the WebSocket completed-candle feed may not
    # provide it for a WS-only candle. REST candles populate it when available.
    open_price = Column("open", Float, nullable=True)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False, default=0)

    data_source = Column(String, nullable=False, default="angel_one_intraday")

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "timestamp",
            name="uq_intraday_ohlc_symbol_timestamp",
        ),
        Index(
            "ix_intraday_ohlc_symbol_timestamp",
            "symbol",
            "timestamp",
        ),
    )


class OptionQuoteHistory(Base):
    """Append-only quote snapshots for later execution-aware signal replay.

    recorded_at is the database capture time, not a claim about exchange
    observation time. Provider and WebSocket timestamps are retained separately.
    """
    __tablename__ = "option_quote_history"

    id = Column(Integer, primary_key=True, index=True)
    recorded_at = Column(DateTime, nullable=False)
    symbol = Column(String, nullable=False)
    expiry = Column(String, nullable=False)
    strike = Column(Float, nullable=False)
    option_type = Column(String, nullable=False)
    last_price = Column(Float, nullable=True)
    bid = Column(Float, nullable=True)
    ask = Column(Float, nullable=True)
    quote_source = Column(String(120), nullable=False)
    exchange_feed_time = Column(String, nullable=True)
    exchange_trade_time = Column(String, nullable=True)
    ws_received_at = Column(Float, nullable=True)
    ws_exchange_timestamp = Column(String, nullable=True)
    ws_exchange_timestamp_ms = Column(String, nullable=True)

    __table_args__ = (
        Index(
            "ix_option_quote_contract_recorded",
            "symbol", "expiry", "strike", "option_type", "recorded_at",
        ),
    )


class ForwardPaperOutcome(Base):
    """Persisted per-ledger, per-horizon replay; never a broker trade."""

    __tablename__ = "forward_paper_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    ledger_id = Column(
        Integer,
        ForeignKey("daily_signal_ledger.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol = Column(String, nullable=False, index=True)
    horizon_minutes = Column(Integer, nullable=False)
    event_timestamp = Column(DateTime, nullable=False)
    target_timestamp = Column(DateTime, nullable=False)
    status = Column(String(40), nullable=False, default="pending", index=True)
    status_reason = Column(Text, nullable=True)
    last_evaluated_at = Column(DateTime, nullable=True)

    entry_source = Column(String(120), nullable=True)
    option_entry_price = Column(Float, nullable=True)
    option_entry_bid = Column(Float, nullable=True)
    option_entry_ask = Column(Float, nullable=True)

    spot_entry_price = Column(Float, nullable=True)
    spot_target_price = Column(Float, nullable=True)
    spot_target_observed_at = Column(DateTime, nullable=True)
    spot_target_offset_seconds = Column(Float, nullable=True)
    spot_return_pct = Column(Float, nullable=True)
    spot_directional_return_pct = Column(Float, nullable=True)

    option_target_ltp = Column(Float, nullable=True)
    option_target_bid = Column(Float, nullable=True)
    option_target_ask = Column(Float, nullable=True)
    option_target_recorded_at = Column(DateTime, nullable=True)
    option_target_offset_seconds = Column(Float, nullable=True)
    option_quote_source = Column(String(120), nullable=True)
    option_ws_received_at = Column(Float, nullable=True)
    option_ws_exchange_timestamp = Column(String, nullable=True)
    option_ws_exchange_timestamp_ms = Column(String, nullable=True)

    premium_raw_return_pct = Column(Float, nullable=True)
    premium_action_adjusted_return_pct = Column(Float, nullable=True)
    execution_aware_return_pct = Column(Float, nullable=True)
    execution_aware_pnl_per_lot = Column(Float, nullable=True)
    mfe_pct = Column(Float, nullable=True)
    mae_pct = Column(Float, nullable=True)
    option_quote_count = Column(Integer, nullable=True)

    details = Column(JSON, nullable=True)
    created_at = Column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime, nullable=False,
        server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "ledger_id",
            "horizon_minutes",
            name="uq_forward_paper_outcome_ledger_horizon",
        ),
        Index(
            "ix_forward_paper_outcome_symbol_horizon_status",
            "symbol", "horizon_minutes", "status",
        ),
        Index(
            "ix_forward_paper_outcome_target_status",
            "target_timestamp", "status",
        ),
    )


class AIProviderEvent(Base):
    """Sanitized runtime telemetry for AI provider attempts; never stores prompts or secrets."""
    __tablename__ = "ai_provider_events"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    provider = Column(String(24), nullable=False, index=True)
    outcome = Column(String(16), nullable=False, index=True)
    latency_ms = Column(Float, nullable=False, default=0)
    error_category = Column(String(40), nullable=True)
    attempt_number = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index("ix_ai_provider_events_timestamp_id", "timestamp", "id"),
    )
