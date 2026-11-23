"""
NIFTY-PRO — global news sentiment (blueprint §3-இ)
===================================================
NewsAPI headlines -> simple lexicon score in [-1, +1]. Only headline TITLES are
scored/returned (no article bodies stored). Needs NEWSAPI_KEY; without it the
result is source="unavailable" (never a fake neutral score).
This is a crude keyword signal — treat as a context hint, not a trading trigger.
"""
import logging
import time
from typing import Dict, List

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_POS = ("surge", "rally", "gain", "rise", "jump", "record high", "beat", "easing", "rate cut", "ceasefire",
        "optimism", "rebound", "upgrade", "strong", "growth", "soar")
_NEG = ("fall", "plunge", "slump", "crash", "drop", "selloff", "sell-off", "war", "tariff", "sanction",
        "recession", "inflation", "rate hike", "downgrade", "weak", "fear", "tension", "attack", "default")
_TTL = 600
_cache: Dict[str, Dict] = {}

QUERIES = {
    "crude": "crude oil OR Brent OR OPEC",
    "gold": "gold price OR safe haven",
    "markets": "stock market OR Federal Reserve OR India economy",
}


def score_titles(titles: List[str]) -> Dict:
    pos = neg = 0
    for t in titles:
        low = (t or "").lower()
        pos += sum(1 for w in _POS if w in low)
        neg += sum(1 for w in _NEG if w in low)
    tot = pos + neg
    return {"score": round((pos - neg) / tot, 2) if tot else 0.0, "positive": pos, "negative": neg, "headlines": len(titles)}


async def get_sentiment(topic: str = "markets") -> Dict:
    if not settings.newsapi_key:
        return {"topic": topic, "source": "unavailable", "reason": "NEWSAPI_KEY அமைக்கப்படவில்லை"}
    hit = _cache.get(topic)
    if hit and time.time() - hit["ts"] < _TTL:
        return hit["data"]
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get("https://newsapi.org/v2/everything",
                            params={"q": QUERIES.get(topic, topic), "language": "en", "sortBy": "publishedAt", "pageSize": 30},
                            headers={"X-Api-Key": settings.newsapi_key})
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        titles = [a.get("title") or "" for a in r.json().get("articles", [])]
        data = {"topic": topic, "source": "newsapi", **score_titles(titles), "sample_titles": titles[:5]}
    except Exception as e:
        logger.warning("NewsAPI failed: %s", type(e).__name__)
        return {"topic": topic, "source": "unavailable", "reason": type(e).__name__}
    _cache[topic] = {"ts": time.time(), "data": data}
    return data
