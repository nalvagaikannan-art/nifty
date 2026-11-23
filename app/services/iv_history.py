"""
NIFTY-PRO — daily ATM-IV history (feeds IV Rank / IV Percentile)
================================================================
One row per (symbol, IST trade date). During market hours the row is
overwritten by the latest value, so the stored number is effectively the
session-close ATM IV — comparable day to day. History accrues by itself
while the app runs (GET /api/pro/overview/* records it).
"""
import logging
from datetime import timedelta
from typing import List

from sqlalchemy import Column, Float, Integer, String, DateTime, UniqueConstraint, select
from sqlalchemy.sql import func

from app.database import Base, AsyncSessionLocal
from app.utils.helpers import now_ist

logger = logging.getLogger(__name__)


class IvHistory(Base):
    __tablename__ = "pro_iv_history"
    __table_args__ = (UniqueConstraint("symbol", "trade_date", name="uq_pro_iv_symbol_date"),)
    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True, nullable=False)
    trade_date = Column(String, nullable=False)          # YYYY-MM-DD (IST)
    atm_iv = Column(Float, nullable=False)
    vix = Column(Float, nullable=True)
    spot = Column(Float, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


def in_market_hours() -> bool:
    n = now_ist()
    if n.weekday() >= 5:
        return False
    mins = n.hour * 60 + n.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


async def record_iv(symbol: str, atm_iv: float, vix: float = 0.0, spot: float = 0.0, force: bool = False) -> bool:
    """Upsert today's ATM IV. Skips outside market hours (stale quotes) unless force."""
    if not atm_iv or atm_iv <= 0 or not (force or in_market_hours()):
        return False
    today = now_ist().date().isoformat()
    sym = symbol.upper()
    try:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(select(IvHistory).where(
                IvHistory.symbol == sym, IvHistory.trade_date == today))).scalar_one_or_none()
            if row:
                row.atm_iv, row.vix, row.spot = atm_iv, vix or row.vix, spot or row.spot
            else:
                s.add(IvHistory(symbol=sym, trade_date=today, atm_iv=atm_iv, vix=vix or None, spot=spot or None))
            await s.commit()
        return True
    except Exception as e:  # history must never break a live request
        logger.warning("IV history write failed for %s: %s", sym, type(e).__name__)
        return False


async def get_iv_values(symbol: str, days: int = 365, exclude_today: bool = True) -> List[float]:
    cutoff = (now_ist().date() - timedelta(days=days)).isoformat()
    today = now_ist().date().isoformat()
    async with AsyncSessionLocal() as s:
        q = select(IvHistory.atm_iv).where(IvHistory.symbol == symbol.upper(), IvHistory.trade_date >= cutoff)
        if exclude_today:
            q = q.where(IvHistory.trade_date != today)
        return [float(v) for (v,) in (await s.execute(q)).all()]
