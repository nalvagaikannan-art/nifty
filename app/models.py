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
