"""
Paper Trading Engine — V2
==========================
Trade open/close/track — real money இல்லாமல் system-ஐ validate செய்கிறோம்.
SQLite-ல் persistent storage.

Status: OPEN → TARGET_1 / TARGET_2 / STOPPED / CLOSED / EXPIRED

API:
    POST /api/paper-trade/open
    POST /api/paper-trade/close
    GET  /api/paper-trade/open
    GET  /api/paper-trade/history
    GET  /api/paper-trade/stats
"""

from __future__ import annotations
import uuid
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import Column, String, Float, Integer, DateTime, JSON, Boolean, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base, AsyncSessionLocal

logger = logging.getLogger(__name__)

# ── Status constants ──────────────────────────────────────────────────────
STATUS_OPEN      = "OPEN"
STATUS_TARGET_1  = "TARGET_1"
STATUS_TARGET_2  = "TARGET_2"
STATUS_STOPPED   = "STOPPED"
STATUS_CLOSED    = "CLOSED"
STATUS_EXPIRED   = "EXPIRED"

LOT_SIZE_MAP = {
    "NIFTY":     50,
    "BANKNIFTY": 15,
    "FINNIFTY":  40,
}


# ── SQLAlchemy Model ──────────────────────────────────────────────────────

class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id                = Column(String, primary_key=True, default=lambda: str(uuid.uuid4())[:8])
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at         = Column(DateTime, nullable=True)

    symbol            = Column(String, nullable=False)
    expiry            = Column(String, nullable=True)
    strike            = Column(Float, nullable=False)
    option_type       = Column(String, nullable=False)   # CE / PE
    side              = Column(String, nullable=False)    # BUY

    entry_price       = Column(Float, nullable=False)
    stop_loss         = Column(Float, nullable=False)
    target_1          = Column(Float, nullable=False)
    target_2          = Column(Float, nullable=False)
    target_3          = Column(Float, nullable=True)

    quantity          = Column(Integer, nullable=False)
    lots              = Column(Integer, nullable=False)

    # Analysis context at time of trade
    signal_strength   = Column(Integer, nullable=True)
    confluence_score  = Column(Integer, nullable=True)
    confluence_quality = Column(String, nullable=True)
    market_regime     = Column(String, nullable=True)
    trigger_level     = Column(Float, nullable=True)
    spot_at_entry     = Column(Float, nullable=True)
    rr_ratio          = Column(Float, nullable=True)
    vix_at_entry      = Column(Float, nullable=True)
    pcr_at_entry      = Column(Float, nullable=True)

    status            = Column(String, default=STATUS_OPEN)
    exit_price        = Column(Float, nullable=True)
    exit_reason       = Column(String, nullable=True)
    pnl               = Column(Float, nullable=True)         # ₹
    pnl_pct           = Column(Float, nullable=True)         # %
    r_multiple        = Column(Float, nullable=True)         # +2R, -1R etc.

    notes             = Column(String, nullable=True)
    trade_metadata    = Column(JSON, nullable=True)


# ── Trade Journal (permanent record) ─────────────────────────────────────

class TradeJournal(Base):
    __tablename__ = "trade_journal"

    id                = Column(String, primary_key=True, default=lambda: str(uuid.uuid4())[:8])
    paper_trade_id    = Column(String, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    closed_at         = Column(DateTime, nullable=True)

    symbol            = Column(String)
    expiry            = Column(String, nullable=True)
    strike            = Column(Float)
    option_type       = Column(String)
    side              = Column(String)

    entry_price       = Column(Float)
    exit_price        = Column(Float, nullable=True)
    stop_loss         = Column(Float)
    target_1          = Column(Float)
    target_2          = Column(Float)
    quantity          = Column(Integer)

    signal_strength   = Column(Integer, nullable=True)
    confluence_score  = Column(Integer, nullable=True)
    market_regime     = Column(String, nullable=True)

    pnl               = Column(Float, nullable=True)
    r_multiple        = Column(Float, nullable=True)
    outcome           = Column(String, nullable=True)    # WIN / LOSS / BREAKEVEN
    exit_reason       = Column(String, nullable=True)


# ── Service functions ──────────────────────────────────────────────────────

async def open_paper_trade(data: Dict) -> Dict:
    """
    New paper trade open செய்கிறோம்.
    data keys: symbol, strike, option_type, entry_price, stop_loss,
               target_1, target_2, quantity, lots,
               signal_strength, confluence_score, market_regime, ...
    """
    async with AsyncSessionLocal() as session:
        lot_size = LOT_SIZE_MAP.get(data.get("symbol", "NIFTY").upper(), 50)
        lots     = data.get("lots", 1)
        qty      = data.get("quantity", lots * lot_size)

        trade = PaperTrade(
            symbol           = data.get("symbol", "NIFTY").upper(),
            expiry           = data.get("expiry"),
            strike           = float(data.get("strike", 0)),
            option_type      = data.get("option_type", "CE").upper(),
            side             = data.get("side", "BUY").upper(),
            entry_price      = float(data.get("entry_price", 0)),
            stop_loss        = float(data.get("stop_loss", 0)),
            target_1         = float(data.get("target_1", 0)),
            target_2         = float(data.get("target_2", 0)),
            target_3         = float(data.get("target_3", 0)) if data.get("target_3") else None,
            quantity         = qty,
            lots             = lots,
            signal_strength  = data.get("signal_strength"),
            confluence_score = data.get("confluence_score"),
            confluence_quality = data.get("confluence_quality"),
            market_regime    = data.get("market_regime"),
            trigger_level    = data.get("trigger_level"),
            spot_at_entry    = data.get("spot_at_entry"),
            rr_ratio         = data.get("rr_ratio"),
            vix_at_entry     = data.get("vix_at_entry"),
            pcr_at_entry     = data.get("pcr_at_entry"),
            notes            = data.get("notes"),
            status           = STATUS_OPEN,
        )
        session.add(trade)
        await session.commit()
        await session.refresh(trade)
        logger.info(f"Paper trade opened: {trade.id} — {trade.symbol} {trade.strike} {trade.option_type}")
        return _trade_to_dict(trade)


async def close_paper_trade(trade_id: str, exit_price: float, exit_reason: str = "MANUAL") -> Dict:
    """
    Paper trade close செய்கிறோம். P&L + R multiple calculate.
    """
    async with AsyncSessionLocal() as session:
        trade = await session.get(PaperTrade, trade_id)
        if not trade:
            return {"error": f"Trade {trade_id} not found"}
        if trade.status != STATUS_OPEN:
            return {"error": f"Trade {trade_id} is already {trade.status}"}

        trade.exit_price   = exit_price
        trade.exit_reason  = exit_reason
        trade.closed_at    = datetime.utcnow()
        trade.updated_at   = datetime.utcnow()

        # P&L
        if trade.side == "BUY":
            pnl_per_unit = exit_price - trade.entry_price
        else:
            pnl_per_unit = trade.entry_price - exit_price

        trade.pnl     = round(pnl_per_unit * trade.quantity, 2)
        trade.pnl_pct = round(pnl_per_unit / trade.entry_price * 100, 2) if trade.entry_price > 0 else 0

        # R multiple
        risk_per_unit = abs(trade.entry_price - trade.stop_loss)
        trade.r_multiple = round(pnl_per_unit / risk_per_unit, 2) if risk_per_unit > 0 else 0

        # Status
        if exit_reason == "STOP_LOSS":
            trade.status = STATUS_STOPPED
        elif exit_reason == "TARGET_1":
            trade.status = STATUS_TARGET_1
        elif exit_reason == "TARGET_2":
            trade.status = STATUS_TARGET_2
        elif exit_reason == "EXPIRED":
            trade.status = STATUS_EXPIRED
        else:
            trade.status = STATUS_CLOSED

        # Save to journal
        outcome = "WIN" if trade.pnl > 0 else "LOSS" if trade.pnl < 0 else "BREAKEVEN"
        journal = TradeJournal(
            paper_trade_id = trade.id,
            symbol         = trade.symbol,
            expiry         = trade.expiry,
            strike         = trade.strike,
            option_type    = trade.option_type,
            side           = trade.side,
            entry_price    = trade.entry_price,
            exit_price     = exit_price,
            stop_loss      = trade.stop_loss,
            target_1       = trade.target_1,
            target_2       = trade.target_2,
            quantity       = trade.quantity,
            signal_strength  = trade.signal_strength,
            confluence_score = trade.confluence_score,
            market_regime    = trade.market_regime,
            pnl              = trade.pnl,
            r_multiple       = trade.r_multiple,
            outcome          = outcome,
            exit_reason      = exit_reason,
            closed_at        = trade.closed_at,
        )
        session.add(journal)
        await session.commit()
        await session.refresh(trade)
        logger.info(f"Paper trade closed: {trade.id} — P&L ₹{trade.pnl:.2f} ({trade.r_multiple:+.2f}R)")
        return _trade_to_dict(trade)


async def get_open_trades(symbol: Optional[str] = None) -> List[Dict]:
    """Open trades list"""
    async with AsyncSessionLocal() as session:
        q = select(PaperTrade).where(PaperTrade.status == STATUS_OPEN)
        if symbol:
            q = q.where(PaperTrade.symbol == symbol.upper())
        result = await session.execute(q.order_by(PaperTrade.created_at.desc()))
        return [_trade_to_dict(t) for t in result.scalars().all()]


async def get_trade_history(symbol: Optional[str] = None, limit: int = 50) -> List[Dict]:
    """Closed trades history"""
    async with AsyncSessionLocal() as session:
        q = select(PaperTrade).where(PaperTrade.status != STATUS_OPEN)
        if symbol:
            q = q.where(PaperTrade.symbol == symbol.upper())
        result = await session.execute(
            q.order_by(PaperTrade.closed_at.desc()).limit(limit)
        )
        return [_trade_to_dict(t) for t in result.scalars().all()]


async def get_paper_trade_stats(symbol: Optional[str] = None) -> Dict:
    """
    Paper trading statistics.
    Signal strength calibration data-க்கான foundation.
    """
    async with AsyncSessionLocal() as session:
        q = select(PaperTrade).where(PaperTrade.status != STATUS_OPEN)
        if symbol:
            q = q.where(PaperTrade.symbol == symbol.upper())
        result = await session.execute(q)
        trades = result.scalars().all()

        if not trades:
            return {
                "total_trades": 0,
                "open_trades":  0,
                "wins":         0,
                "losses":       0,
                "win_rate":     None,
                "total_pnl":    0,
                "avg_r":        None,
                "by_regime":    {},
                "by_strength":  {},
                "calibration_note": "Not enough trades for calibration. Need 100+ trades.",
            }

        wins   = [t for t in trades if (t.pnl or 0) > 0]
        losses = [t for t in trades if (t.pnl or 0) < 0]
        total_pnl = sum(t.pnl or 0 for t in trades)
        avg_r = sum(t.r_multiple or 0 for t in trades) / len(trades)

        # Count open trades
        open_q = await session.execute(
            select(func.count()).select_from(PaperTrade).where(PaperTrade.status == STATUS_OPEN)
        )
        open_count = open_q.scalar() or 0

        # By market regime
        by_regime: Dict = {}
        for t in trades:
            regime = t.market_regime or "UNKNOWN"
            if regime not in by_regime:
                by_regime[regime] = {"trades": 0, "wins": 0, "pnl": 0.0}
            by_regime[regime]["trades"] += 1
            if (t.pnl or 0) > 0:
                by_regime[regime]["wins"] += 1
            by_regime[regime]["pnl"] += t.pnl or 0

        for r in by_regime.values():
            r["win_rate"] = round(r["wins"] / r["trades"] * 100, 1) if r["trades"] > 0 else 0

        # Signal strength calibration (50-59, 60-69, 70-79, 80-89, 90+)
        buckets = {
            "50-59": {"trades": 0, "wins": 0},
            "60-69": {"trades": 0, "wins": 0},
            "70-79": {"trades": 0, "wins": 0},
            "80-89": {"trades": 0, "wins": 0},
            "90+":   {"trades": 0, "wins": 0},
        }
        for t in trades:
            ss = t.signal_strength or 0
            key = None
            if 50 <= ss <= 59:   key = "50-59"
            elif 60 <= ss <= 69: key = "60-69"
            elif 70 <= ss <= 79: key = "70-79"
            elif 80 <= ss <= 89: key = "80-89"
            elif ss >= 90:       key = "90+"
            if key:
                buckets[key]["trades"] += 1
                if (t.pnl or 0) > 0:
                    buckets[key]["wins"] += 1

        for b in buckets.values():
            b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] >= 5 else None
            b["calibrated"] = b["trades"] >= 20

        calibration_ready = sum(1 for b in buckets.values() if b.get("calibrated")) >= 3

        return {
            "total_trades":     len(trades),
            "open_trades":      open_count,
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate":         round(len(wins) / len(trades) * 100, 1) if trades else None,
            "total_pnl":        round(total_pnl, 2),
            "avg_r":            round(avg_r, 2),
            "best_r":           round(max((t.r_multiple or 0) for t in trades), 2),
            "worst_r":          round(min((t.r_multiple or 0) for t in trades), 2),
            "by_regime":        by_regime,
            "by_strength":      buckets,
            "calibration_ready": calibration_ready,
            "calibration_note": (
                "Signal strength calibration active — win probability estimates available."
                if calibration_ready else
                f"Need more trades for calibration. Current: {len(trades)}, target: 100+"
            ),
        }


async def get_daily_pnl() -> float:
    """Today's realized P&L from paper trades"""
    async with AsyncSessionLocal() as session:
        today = datetime.utcnow().date()
        q = select(PaperTrade).where(
            PaperTrade.status != STATUS_OPEN,
            func.date(PaperTrade.closed_at) == today
        )
        result = await session.execute(q)
        trades = result.scalars().all()
        return sum(t.pnl or 0 for t in trades)


def _trade_to_dict(trade: PaperTrade) -> Dict:
    return {
        "id":               trade.id,
        "created_at":       trade.created_at.isoformat() if trade.created_at else None,
        "closed_at":        trade.closed_at.isoformat() if trade.closed_at else None,
        "symbol":           trade.symbol,
        "expiry":           trade.expiry,
        "strike":           trade.strike,
        "option_type":      trade.option_type,
        "side":             trade.side,
        "entry_price":      trade.entry_price,
        "stop_loss":        trade.stop_loss,
        "target_1":         trade.target_1,
        "target_2":         trade.target_2,
        "target_3":         trade.target_3,
        "quantity":         trade.quantity,
        "lots":             trade.lots,
        "status":           trade.status,
        "exit_price":       trade.exit_price,
        "exit_reason":      trade.exit_reason,
        "pnl":              trade.pnl,
        "pnl_pct":          trade.pnl_pct,
        "r_multiple":       trade.r_multiple,
        "signal_strength":  trade.signal_strength,
        "confluence_score": trade.confluence_score,
        "confluence_quality": trade.confluence_quality,
        "market_regime":    trade.market_regime,
        "trigger_level":    trade.trigger_level,
        "spot_at_entry":    trade.spot_at_entry,
        "rr_ratio":         trade.rr_ratio,
        "vix_at_entry":     trade.vix_at_entry,
        "notes":            trade.notes,
    }
