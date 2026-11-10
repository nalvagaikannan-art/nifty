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
import math
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import AnalysisResult, MarketData
from app.services.accuracy_time import analysis_event_timestamp
from app.utils.helpers import now_utc_naive

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
    cutoff = now_utc_naive() - timedelta(days=days)

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

    # Non-directional metrics intentionally use their own definitions.
    # They must NOT be treated as missing just because their live reason is ⚪.
    nondirectional = {
        "max_pain": {"hits": 0, "total": 0},
        "atr_risk": {"hits": 0, "total": 0},
        "india_vix": {"hits": 0, "total": 0},
    }

    overall_hits, overall_total = 0, 0
    snapshots_used = 0

    if len(prices) >= 2:
        for row in analysis_rows:
            result = row.result or {}
            reasons = result.get("all_reasons", [])
            if len(reasons) < len(INDICATOR_ORDER):
                continue

            indicator_reasons = reasons[:len(INDICATOR_ORDER)]

            ts = analysis_event_timestamp(row)
            if ts is None:
                continue

            price_then = _nearest_price(
                prices, ts, max_gap_minutes=HORIZON_TOLERANCE_MIN
            )
            price_later = _nearest_price(
                prices,
                ts + timedelta(minutes=HORIZON_MINUTES),
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
                actual = None

            # Keep the original directional snapshot semantics:
            # only non-flat moves are counted for directional accuracy.
            if actual is not None:
                snapshots_used += 1

                bias = (result.get("market_bias") or "").lower()
                if bias in ("bullish", "bearish"):
                    predicted_overall = "bull" if bias == "bullish" else "bear"
                    overall_total += 1
                    if predicted_overall == actual:
                        overall_hits += 1

            # --------------------------------------------------------
            # MAX PAIN
            # Metric = did spot become closer to Max Pain after 60m?
            # This is distance alignment, NOT directional accuracy.
            # Flat moves are valid observations here.
            # --------------------------------------------------------
            try:
                max_pain = float(result.get("max_pain") or 0)
            except (TypeError, ValueError):
                max_pain = 0.0

            if max_pain > 0:
                nondirectional["max_pain"]["total"] += 1
                if abs(price_later - max_pain) < abs(price_then - max_pain):
                    nondirectional["max_pain"]["hits"] += 1

            # --------------------------------------------------------
            # ATR
            # Metric = did the absolute 60m spot move stay within
            # one ATR measured at analysis time?
            # --------------------------------------------------------
            technicals = result.get("technicals") or {}
            try:
                atr = float(technicals.get("atr") or 0)
            except (TypeError, ValueError):
                atr = 0.0

            if atr > 0:
                nondirectional["atr_risk"]["total"] += 1
                if abs(price_later - price_then) <= atr:
                    nondirectional["atr_risk"]["hits"] += 1

            # --------------------------------------------------------
            # INDIA VIX
            # Metric = did the absolute 60m move stay within the
            # one-sigma VIX-implied one-hour expected move?
            # VIX is annualized volatility, not direction.
            # --------------------------------------------------------
            try:
                vix = float(result.get("vix") or 0)
            except (TypeError, ValueError):
                vix = 0.0

            if vix > 0:
                expected_move = (
                    price_then
                    * (vix / 100.0)
                    / math.sqrt(252.0 * 6.25)
                )
                if expected_move > 0:
                    nondirectional["india_vix"]["total"] += 1
                    if abs(price_later - price_then) <= expected_move:
                        nondirectional["india_vix"]["hits"] += 1

            # Directional indicators only.
            for idx, (ind_id, _, _) in enumerate(INDICATOR_ORDER):
                if ind_id in nondirectional:
                    continue

                # Flat moves are skipped only for directional indicators.
                if actual is None:
                    continue

                predicted = _parse_direction(indicator_reasons[idx])
                if predicted == "neutral":
                    continue

                totals[ind_id]["total"] += 1
                if predicted == actual:
                    totals[ind_id]["hits"] += 1

    indicators: List[Dict] = []

    metric_labels = {
        "max_pain": "Moved closer to Max Pain",
        "atr_risk": "60m move within 1× ATR",
        "india_vix": "60m move within 1× VIX expected move",
    }

    metric_definitions = {
        "max_pain": (
            "Absolute distance from Max Pain was smaller at the "
            "60-minute check."
        ),
        "atr_risk": (
            "Absolute 60-minute spot move was less than or equal "
            "to ATR at analysis time."
        ),
        "india_vix": (
            "Absolute 60-minute spot move was less than or equal "
            "to the one-sigma VIX-implied one-hour move."
        ),
    }

    for ind_id, icon, title in INDICATOR_ORDER:

        if ind_id in nondirectional:
            hits = nondirectional[ind_id]["hits"]
            total = nondirectional[ind_id]["total"]
            rate = round(hits / total * 100, 1) if total > 0 else None

            indicators.append({
                "id": ind_id,
                "icon": icon,
                "title_ta": title,
                "hits": hits,
                "total": total,
                "success_rate": rate,
                "insufficient_data": total < MIN_SIGNALS_FOR_CONFIDENCE,
                "metric_type": "non_directional",
                "metric_label": metric_labels[ind_id],
                "metric_definition": metric_definitions[ind_id],
                "horizon_minutes": HORIZON_MINUTES,
            })
            continue

        hits = totals[ind_id]["hits"]
        total = totals[ind_id]["total"]
        rate = round(hits / total * 100, 1) if total > 0 else None

        indicators.append({
            "id": ind_id,
            "icon": icon,
            "title_ta": title,
            "hits": hits,
            "total": total,
            "success_rate": rate,
            "insufficient_data": total < MIN_SIGNALS_FOR_CONFIDENCE,
            "metric_type": "directional",
        })

    # Directional indicators first; non-directional metrics at the end.
    indicators.sort(
        key=lambda x: (
            x["metric_type"] != "directional",
            x["success_rate"] is None,
            -(x["success_rate"] or 0),
        )
    )

    overall_rate = (
        round(overall_hits / overall_total * 100, 1)
        if overall_total > 0
        else None
    )

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

