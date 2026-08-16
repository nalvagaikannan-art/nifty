"""
Persistence layer for market snapshots and AI/technical analysis results.

BEFORE THIS FILE EXISTED: app/models.py defined MarketData, OptionData, and
AnalysisResult tables, and app/database.py created them on startup — but no
code anywhere ever wrote a row to them. Tables existed but stayed empty
forever. This module is what actually saves history, and the API routes now
call it.
"""
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import logging

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import MarketData, OptionData, AnalysisResult

logger = logging.getLogger(__name__)


async def save_market_snapshot(spot: Dict) -> None:
    """Persist a single spot-price snapshot. Failures are logged, not raised —
    a DB hiccup should never take down a live price request."""
    try:
        async with AsyncSessionLocal() as session:
            price = spot.get("price")
            if price is None:
                return
            row = MarketData(
                symbol=str(spot.get("symbol", "")).upper(),
                price=float(price),
                timestamp=datetime.utcnow(),
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to save market snapshot for {spot.get('symbol')}: {e}")


async def save_option_chain_snapshot(symbol: str, expiry: str, option_df) -> None:
    """Persist one row per strike/side for the current option chain snapshot.
    Used to build the OI-change-over-time history that get_oi_change() needs
    (currently a placeholder — see production checklist)."""
    if option_df is None or option_df.empty:
        return
    try:
        async with AsyncSessionLocal() as session:
            rows = []
            for _, r in option_df.iterrows():
                snap_ts = datetime.utcnow()
                rows.append(OptionData(
                    symbol=symbol, expiry=expiry, strike=float(r["strike"]), option_type="CE",
                    last_price=float(r["ce_ltp"]), volume=int(r["ce_volume"]),
                    open_interest=int(r["ce_oi"]), implied_volatility=float(r["ce_iv"]),
                    timestamp=snap_ts,
                ))
                rows.append(OptionData(
                    symbol=symbol, expiry=expiry, strike=float(r["strike"]), option_type="PE",
                    last_price=float(r["pe_ltp"]), volume=int(r["pe_volume"]),
                    open_interest=int(r["pe_oi"]), implied_volatility=float(r["pe_iv"]),
                    timestamp=snap_ts,
                ))
            session.add_all(rows)
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to save option chain snapshot for {symbol}: {e}")


async def get_oi_change_since(symbol: str, expiry: str, current_df, minutes_ago: int = 15) -> Dict:
    """
    Real OI-change-over-time: compares the CURRENT option-chain snapshot
    (current_df, already fetched this request) against the most recent
    snapshot this app itself saved at least `minutes_ago` minutes ago —
    not the provider's single "today's change" field, which only tells
    you the change since yesterday's close, not "what happened in the
    last 15 minutes".

    Classifies each strike/side using OI-delta + price-delta together
    (the standard convention):
      OI up   + price up   → Long Buildup
      OI up   + price down → Short Buildup
      OI down + price up   → Short Covering
      OI down + price down → Long Unwinding

    Returns {"available": False, ...} when there isn't yet a snapshot old
    enough to compare against (e.g. app just started) — callers should
    show "building history..." rather than a fabricated comparison.
    """
    if current_df is None or current_df.empty:
        return {"available": False, "reason": "no current option chain"}

    cutoff = datetime.utcnow() - timedelta(minutes=minutes_ago)
    async with AsyncSessionLocal() as session:
        stmt = (
            select(OptionData)
            .where(OptionData.symbol == symbol, OptionData.expiry == expiry, OptionData.timestamp <= cutoff)
            .order_by(OptionData.timestamp.desc())
            .limit(500)
        )
        rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        return {"available": False, "reason": f"no snapshot older than {minutes_ago}m yet — history still building"}

    ref_ts = rows[0].timestamp
    snapshot_rows = [r for r in rows if ref_ts and r.timestamp and abs((ref_ts - r.timestamp).total_seconds()) <= 5]
    prev_map = {(r.strike, r.option_type): (r.open_interest or 0, r.last_price or 0) for r in snapshot_rows}

    actual_minutes = round((datetime.utcnow() - ref_ts).total_seconds() / 60, 1) if ref_ts else minutes_ago

    ce_delta_total = 0
    pe_delta_total = 0
    per_strike: List[Dict] = []

    for _, row in current_df.iterrows():
        strike = float(row["strike"])
        for side, oi_col, price_col in (("CE", "ce_oi", "ce_ltp"), ("PE", "pe_oi", "pe_ltp")):
            prev = prev_map.get((strike, side))
            if prev is None:
                continue
            prev_oi, prev_price = prev
            cur_oi, cur_price = int(row[oi_col]), float(row[price_col])
            oi_delta = cur_oi - int(prev_oi)
            price_delta = cur_price - float(prev_price)
            if oi_delta > 0 and price_delta > 0:
                label = "Long Buildup"
            elif oi_delta > 0 and price_delta < 0:
                label = "Short Buildup"
            elif oi_delta < 0 and price_delta > 0:
                label = "Short Covering"
            elif oi_delta < 0 and price_delta < 0:
                label = "Long Unwinding"
            else:
                label = "Flat"
            if side == "CE":
                ce_delta_total += oi_delta
            else:
                pe_delta_total += oi_delta
            if oi_delta != 0:
                per_strike.append({
                    "strike": strike, "side": side, "oi_change": oi_delta,
                    "price_change": round(price_delta, 2), "label": label,
                })

    per_strike.sort(key=lambda x: abs(x["oi_change"]), reverse=True)

    return {
        "available": True,
        "window_minutes": actual_minutes,
        "ce_oi_change": ce_delta_total,
        "pe_oi_change": pe_delta_total,
        "top_buildups": per_strike[:8],
    }


async def save_analysis_result(symbol: str, analysis_type: str, result: Dict) -> None:
    """Persist an AI / technical / risk analysis result for later review and
    for computing model accuracy/backtests over time."""
    try:
        async with AsyncSessionLocal() as session:
            row = AnalysisResult(
                symbol=symbol,
                analysis_type=analysis_type,
                result=result,
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to save {analysis_type} analysis result for {symbol}: {e}")
