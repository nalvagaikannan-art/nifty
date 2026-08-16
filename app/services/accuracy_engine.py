"""
Indicator Accuracy Engine
=========================
கடந்த N நாட்களில் ஒவ்வொரு indicator-ம் எவ்வளவு சரியா வேலை செய்தது-ன்னு
கணக்கிடும் — stored AnalysisResult rows (ஒவ்வொரு /api/analysis/ai/{symbol}
call-லயும் save ஆகும் "all_reasons" list) vs stored MarketData spot-price
snapshots (dashboard load ஆகும்போது save ஆகும்) ஒப்பிட்டு.

Method:
  ஒவ்வொரு saved analysis snapshot-க்கும் (time T, 20 indicator directions):
    price_then  = T-க்கு அருகிலான spot price
    price_later = T + HORIZON_MINUTES-க்கு அருகிலான spot price (window-க்குள்)
    actual = 'bull' if price ஏறிருந்தா (> +NEUTRAL_BAND%),
             'bear' if இறங்கிருந்தா (< -NEUTRAL_BAND%),
             'neutral' இல்லனா (skip — சின்ன moves-ஐ கணக்கில் எடுக்கல)
  ஒவ்வொரு indicator-க்கும், அது 'bull'/'bear' predict பண்ணி இருந்தா
  (neutral predictions கணக்கில் எடுக்கல்) actual-ஓடு ஒப்பிட்டு hit/miss
  தீர்மானிக்கப்படும்.

NOTE: இது small-sample, best-effort backtest — analysis calls எவ்வளவு
அடிக்கடி நடந்துச்சு, dashboard எவ்வளவு அடிக்கடி load ஆச்சு-ன்னு data
density-ஐ பொறுத்து துல்லியம் மாறும். "insufficient_data" flag குறைந்த
sample-க்கு காட்டப்படும்.
"""
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import AnalysisResult, MarketData

logger = logging.getLogger(__name__)

HORIZON_MINUTES = 60          # "பின்னால் விலை" எந்த தூரத்தில் பார்க்கணும்
HORIZON_TOLERANCE_MIN = 45    # horizon-ஐ சுத்தி இவ்வளவு window-க்குள் price snapshot தேடும்
NEUTRAL_BAND_PCT = 0.05       # இதுக்கு கீழ movement = neutral (சத்தமா எடுத்துக்கல)
MIN_SIGNALS_FOR_CONFIDENCE = 5

# decision_engine.run_decision_engine() ஒவ்வொரு தடவையும் இதே fixed order-ல
# 20 reasons append பண்ணும் — அதே order இங்க வேணும்.
INDICATOR_ORDER: List[Tuple[str, str, str]] = [
    ("pcr",             "⚖️", "புட்-கால் விகிதம் (PCR)"),
    ("oi_change",       "📊", "ஓபன் இன்ட்ரெஸ்ட் மாற்றம் (OI Change)"),
    ("max_pain",        "🎯", "மேக்ஸ் பெயின் (Max Pain)"),
    ("call_writing",    "🔴", "கால் ரைட்டிங் (Resistance)"),
    ("put_writing",     "🟢", "புட் ரைட்டிங் (Support)"),
    ("futures_premium", "📈", "ஃப்யூச்சர்ஸ் பிரீமியம்"),
    ("vwap",            "⚡", "வி-வேப் (VWAP)"),
    ("ema20",           "📉", "EMA20"),
    ("ema50",           "📉", "EMA50"),
    ("rsi",             "🌡️", "ஆர்எஸ்ஐ (RSI)"),
    ("macd",            "〰️", "மேக்டி (MACD)"),
    ("adx",             "💪", "ஏடிஎக்ஸ் (ADX)"),
    ("atr_risk",        "📏", "ஏடிஆர் (ATR)"),
    ("supertrend",      "🎯", "சூப்பர்டிரெண்ட்"),
    ("volume_spike",    "📶", "வால்யூம் ஸ்பைக்"),
    ("india_vix",       "😨", "இந்தியா விக்ஸ் (VIX)"),
    ("global_market",   "🌍", "குளோபல் மார்க்கெட்"),
    ("gift_nifty",      "🎁", "கிஃப்ட் நிஃப்டி"),
    ("fii",             "🌐", "எஃப்ஐஐ (FII)"),
    ("dii",             "🏛️", "டிஐஐ (DII)"),
]

EMOJI_DIRECTION = {"🟢": "bull", "🔴": "bear", "⚪": "neutral"}


def _parse_direction(reason_line: str) -> str:
    for emoji, direction in EMOJI_DIRECTION.items():
        if reason_line.startswith(emoji):
            return direction
    return "neutral"


def _nearest_price(prices: List[Tuple[datetime, float]], target: datetime,
                    max_gap_minutes: Optional[float] = None) -> Optional[float]:
    """prices ஒரு (timestamp, price) list, timestamp-படி sorted ஆ இருக்கணும்."""
    best_price, best_gap = None, None
    for ts, price in prices:
        gap = abs((ts - target).total_seconds()) / 60.0
        if best_gap is None or gap < best_gap:
            best_gap, best_price = gap, price
    if best_price is None:
        return None
    if max_gap_minutes is not None and best_gap > max_gap_minutes:
        return None
    return best_price


async def compute_indicator_accuracy(symbol: str, days: int = 15) -> Dict:
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with AsyncSessionLocal() as session:
        analysis_rows = (await session.execute(
            select(AnalysisResult)
            .where(AnalysisResult.symbol == symbol)
            .where(AnalysisResult.analysis_type == "ai")
            .where(AnalysisResult.timestamp >= cutoff)
            .order_by(AnalysisResult.timestamp.asc())
        )).scalars().all()

        market_rows = (await session.execute(
            select(MarketData)
            .where(MarketData.symbol == symbol)
            .where(MarketData.timestamp >= cutoff)
            .order_by(MarketData.timestamp.asc())
        )).scalars().all()

    prices: List[Tuple[datetime, float]] = [
        (r.timestamp, r.price) for r in market_rows if r.price is not None
    ]

    totals: Dict[str, Dict[str, int]] = {
        ind_id: {"hits": 0, "total": 0} for ind_id, _, _ in INDICATOR_ORDER
    }
    overall_hits, overall_total = 0, 0
    snapshots_used = 0

    if len(prices) >= 2:
        for row in analysis_rows:
            result = row.result or {}
            reasons = result.get("all_reasons", [])
            if len(reasons) != 20:
                continue  # இந்த feature வர முன் save ஆன பழைய rows — skip

            ts = row.timestamp
            price_then = _nearest_price(prices, ts, max_gap_minutes=HORIZON_TOLERANCE_MIN)
            price_later = _nearest_price(
                prices, ts + timedelta(minutes=HORIZON_MINUTES),
                max_gap_minutes=HORIZON_TOLERANCE_MIN,
            )
            if price_then is None or price_later is None or price_then <= 0:
                continue

            change_pct = (price_later - price_then) / price_then * 100
            if change_pct > NEUTRAL_BAND_PCT:
                actual = "bull"
            elif change_pct < -NEUTRAL_BAND_PCT:
                actual = "bear"
            else:
                continue  # flat move — indicator-க்கு fair-ஆ credit/debit பண்ண முடியாது

            snapshots_used += 1

            # Overall market_bias accuracy
            bias = (result.get("market_bias") or "").lower()
            if bias in ("bullish", "bearish"):
                predicted_overall = "bull" if bias == "bullish" else "bear"
                overall_total += 1
                if predicted_overall == actual:
                    overall_hits += 1

            for idx, (ind_id, _, _) in enumerate(INDICATOR_ORDER):
                predicted = _parse_direction(reasons[idx])
                if predicted == "neutral":
                    continue
                totals[ind_id]["total"] += 1
                if predicted == actual:
                    totals[ind_id]["hits"] += 1

    indicators: List[Dict] = []
    for ind_id, icon, title in INDICATOR_ORDER:
        hits, total = totals[ind_id]["hits"], totals[ind_id]["total"]
        rate = round(hits / total * 100, 1) if total > 0 else None
        indicators.append({
            "id": ind_id,
            "icon": icon,
            "title_ta": title,
            "hits": hits,
            "total": total,
            "success_rate": rate,
            "insufficient_data": total < MIN_SIGNALS_FOR_CONFIDENCE,
        })

    # அதிக success rate முதலில் வரும் மாதிரி sort — data இல்லாதவை கடைசில்
    indicators.sort(key=lambda x: (x["success_rate"] is None, -(x["success_rate"] or 0)))

    overall_rate = round(overall_hits / overall_total * 100, 1) if overall_total > 0 else None

    return {
        "symbol": symbol,
        "days": days,
        "horizon_minutes": HORIZON_MINUTES,
        "snapshots_used": snapshots_used,
        "overall": {
            "hits": overall_hits,
            "total": overall_total,
            "success_rate": overall_rate,
            "insufficient_data": overall_total < MIN_SIGNALS_FOR_CONFIDENCE,
        },
        "indicators": indicators,
    }
