from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Text
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

