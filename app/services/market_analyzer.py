"""
MarketAnalyzer — Full pipeline:
  DataFetcher → TechnicalIndicators → DecisionEngine → AIEngine (reasoning only)
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from app.services.data_fetcher import DataFetcher
from app.services.technical_indicators import TechnicalIndicators
from app.services.option_analyzer import OptionAnalyzer
from app.services.decision_engine import run_decision_engine
from app.services.strategy_engine import generate_option_strategy, generate_price_levels
from app.services.history_service import save_option_chain_snapshot
from app.services.global_market import global_market_service
from app.services.tamil_explainer import build_tamil_indicators
from app.services import history_service
from app.utils.helpers import safe_float, expiry_filter
from app.utils.cache import async_cache
from app.config import settings

logger = logging.getLogger(__name__)


def _sanitize(obj):
    """
    numpy.bool_ / numpy.int64 / numpy.float64 — FastAPI JSON serialize
    செய்ய முடியாது. Recursive-ஆக Python native types-ஆக மாற்றுகிறோம்.
    volume_spike (numpy.bool_) இதன் மூலம் bool ஆகும்.
    """
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


class MarketAnalyzer:
    def __init__(self, fetcher: Optional[DataFetcher] = None):
        self.fetcher = fetcher or DataFetcher()
        self._owns_fetcher = fetcher is None
        self.tech   = TechnicalIndicators()
        self.option = OptionAnalyzer()

    # async_cache provides single-flight deduplication: concurrent callers
    # for the same symbol/expiry share one expensive market-data fetch.
    @async_cache(ttl=settings.analysis_cache_ttl)
    async def get_full_market_overview(self, symbol: str, expiry: str = None) -> Dict:
        """
        Concurrent fetch → process → decision engine → return.
        AI reasoning is called separately from /api/analysis/ai/{symbol}.

        Cached for `settings.analysis_cache_ttl` seconds (default 15s) keyed
        by symbol (and expiry, when given — async_cache's key includes every
        arg) — /api/analysis/ai/{symbol} and /api/strategy/recommend/
        {symbol} both call this independently on every Analysis-page load,
        and without this cache each one re-runs the entire fetch pipeline
        (spot + option chain + VIX + breadth + historical + global + FII/DII
        + intraday multi-timeframe + option-chain DB snapshot), which is what
        makes the page hang on "Loading...". Both calls within the TTL window
        now share one snapshot instead of duplicating all of that work.

        `expiry`: optional, e.g. "28-Aug-2025" (same format as
        option_chain.all_expiries). None (default) → nearest expiry, same
        as before. Passed straight through to the option-chain fetch so the
        whole pipeline below (PCR, max pain, OI walls, decision engine,
        strategy/strikes, expiry-day risk) reflects the requested expiry
        instead of always the nearest one.
        """
        # ── 1. Concurrent data fetch ─────────────────────────────────────
        # spot MUST succeed (it's the whole point of the request) — if both
        # Angel One and NSE fail, that's a genuine "no data available" error
        # and should surface as one. Chain/VIX/breadth are enrichment data;
        # each is wrapped so ONE of them failing (e.g. NSE-only option chain
        # blocked on this host) doesn't take the whole gather() down with it
        # — previously a single failed task cancelled every other task in
        # the gather, so option-chain failing meant the dashboard showed
        # nothing at all even though spot price was available.
        import time

        async def _timed(label, coro):
            t0 = time.perf_counter()
            try:
                return await coro
            finally:
                logger.info(
                    "market overview component timing: symbol=%s component=%s elapsed=%.3fs",
                    symbol,
                    label,
                    time.perf_counter() - t0,
                )

        (
            spot, chain, vix, breadth, hist, global_snap, fii_dii,
            multi_tf, futures_data,
        ) = await asyncio.gather(
            _timed("spot", self.fetcher.get_spot(symbol)),
            _timed("option_chain", self._safe_option_chain(symbol, expiry)),
            _timed("volatility", self._safe_volatility()),
            _timed("breadth", self._safe_breadth()),
            _timed("historical", self._safe_historical(symbol)),
            _timed("global_market", self._safe_global_market()),
            _timed("fii_dii", self._safe_fii_dii()),
            _timed("multi_timeframe", self._safe_multi_timeframe(symbol)),
            _timed("futures_premium", self._safe_futures_premium(symbol)),
        )

        # ── 2. Option chain analysis ──────────────────────────────────────
        opt_df    = self.option.process_option_chain(chain)
        pcr       = self.option.compute_pcr(opt_df)
        max_pain  = self.option.compute_max_pain(opt_df)
        oi_change = self.option.compute_oi_change(opt_df)   # provider's own day-change field
        # Pass spot price so oi_summary computes ATM-level OI change
        # (ce_oi_chg_atm / pe_oi_chg_atm) for buildup classification in
        # decision_engine._score_call_writing / _score_put_writing.
        oi_summary = self.option.oi_summary(opt_df, underlying=spot["price"])

        expiry = chain.get("expiry", "")
        await save_option_chain_snapshot(chain.get("symbol", symbol), expiry, opt_df)

        # Our OWN OI-change-over-time, computed from snapshots this app has
        # actually saved (not the provider's single day-change field) — e.g.
        # "what changed in the last ~15 minutes", with buildup classification
        # (long/short buildup, long unwinding, short covering) per side.
        oi_change_tracked = await self._safe_oi_change_tracked(
            chain.get("symbol", symbol), expiry, opt_df
        )

        # ── 3. Technical indicators ───────────────────────────────────────
        prices  = hist.get("closes", [])
        volumes = hist.get("volumes", [])

        if prices and len(prices) >= 20:
            technicals = self.tech.compute_all(prices, volumes if volumes else None)
            trend      = self.tech.trend_detection(prices)
            daily_sr   = self.tech.compute_pivot_support_resistance(prices, spot["price"])
            tech_src   = "historical_daily_close"
        else:
            technicals = self.tech._empty()
            trend      = "sideways"
            daily_sr   = self.tech.compute_support_resistance(spot["price"])
            tech_src   = "placeholder"

        # Prefer the 5-minute timeframe's real OHLC-based indicators (true
        # Wilder ADX/ATR/Supertrend + session VWAP with real volume) when
        # either broker's intraday candles are available (Angel One first,
        # Zerodha as fallback — see data_fetcher.get_intraday_ohlc) — these
        # describe today's actual intraday state, unlike the daily-close
        # approximation above. The daily numbers are kept as
        # `technicals_daily` for reference (used for the "Daily" row in the
        # multi-timeframe panel).
        technicals_daily = technicals
        five_min = (multi_tf or {}).get("5min") or {}
        if five_min.get("data_source") in ("angel_one_intraday", "zerodha_intraday") and five_min.get("indicators"):
            technicals = five_min["indicators"]
            tech_src   = "intraday_5min_ohlc"

        # Support/Resistance: combine pivot levels with option-chain OI
        # walls (Put max-OI = support, Call max-OI = resistance) rather
        # than a single source — see technical_indicators.combine_support_resistance.
        sr = self.tech.combine_support_resistance(
            daily_sr, oi_summary, spot["price"],
            vwap=technicals.get("vwap", 0.0),
        )

        # ── 4. Build full market_data dict ────────────────────────────────
        # Global cues / Gift Nifty / FII / DII: global_market_service and
        # get_fii_dii() both already distinguish "genuinely flat" (a real
        # 0.0-ish number) from "couldn't fetch" (None / source=unavailable).
        # We now keep that distinction all the way through instead of
        # collapsing None → 0.0 here, which used to make "data unavailable"
        # indistinguishable from "global market flat" / "FII net zero" to
        # both the decision engine's reason text and the UI.
        global_val = global_snap.get("global_change_pct")
        gift_val   = global_snap.get("gift_nifty_change_pct")
        fii_raw    = (fii_dii.get("fii") or {}).get("net_value")
        dii_raw    = (fii_dii.get("dii") or {}).get("net_value")

        market_data = {
            "symbol":        symbol,
            "spot":          spot,
            "option_chain":  chain,
            # Option chain validity flag — True only when data was actually
            # fetched (non-empty rows). Used by decision_engine's critical
            # data gate to block CALL/PUT signals when chain is unavailable.
            # PCR > 0 is the gate's primary check; this flag is for UI display.
            "option_chain_valid": bool(chain.get("data")),
            "vix":           vix,
            "breadth":       breadth,
            "pcr":           pcr,
            "max_pain":      max_pain,
            "oi_change":     oi_change,
            "oi_change_tracked": oi_change_tracked,
            "oi_summary":    oi_summary,
            "support_resistance": sr,
            "trend":         trend,
            "rsi":           technicals.get("rsi", 50.0),
            "macd":          technicals.get("macd", {}),
            "technicals":    technicals,
            "technicals_daily": technicals_daily,
            "technical_data_source": tech_src,
            "multi_timeframe": multi_tf,
            "futures_premium":        futures_data.get("premium", 0.0),
            "futures_premium_pct":    futures_data.get("premium_pct", 0.0),
            "futures_premium_status": futures_data.get("status", "unavailable"),
            "global_change_pct":      global_val if global_val is not None else 0.0,
            "global_status":          "live" if global_val is not None else "unavailable",
            "gift_nifty_change_pct":  gift_val if gift_val is not None else 0.0,
            "gift_status":            "live" if gift_val is not None else "unavailable",
            "global_markets":         global_snap.get("instruments", {}),
            "fii_net_cr":             safe_float(fii_raw) if fii_raw is not None else 0.0,
            "fii_status":             "live" if fii_raw is not None else "unavailable",
            "dii_net_cr":             safe_float(dii_raw) if dii_raw is not None else 0.0,
            "dii_status":             "live" if dii_raw is not None else "unavailable",
            "fii_dii":               fii_dii,
            "market_open":           spot.get("market_open", True),
            "expiry_risk":           expiry_filter(expiry),
            "timestamp":     pd.Timestamp.now().isoformat(),
        }

        # ── 5. Rule-based Decision Engine ─────────────────────────────────
        decision = run_decision_engine(market_data)
        market_data["decision"] = decision

        # ── 6. Concrete strategy (strike/premium/SL/target or multi-leg) ──
        # Fills in whatever strategy *name* the decision engine picked with
        # real numbers from this request's opt_df — suggestion only, no
        # order is placed. None if there's no clear pick or the needed
        # strikes aren't quoting (see strategy_engine.generate_option_strategy).
        market_data["decision"]["strategy_detail"] = generate_option_strategy(market_data, opt_df)
        market_data["decision"]["price_levels"] = generate_price_levels(market_data)

        # Tamil indicator explanations (app/services/tamil_explainer.py) —
        # this module already existed with full Tamil explanations for every
        # indicator (PCR, OI, VWAP, RSI, MACD, ADX, Supertrend, VIX, FII/DII,
        # Global/Gift Nifty etc.) but nothing ever called it, so the UI never
        # showed "இதன் அர்த்தம் என்ன" for any indicator. Wiring it in here
        # makes it available to every caller of get_full_market_overview.
        market_data["tamil_indicators"] = build_tamil_indicators(market_data, decision)

        # Live option-chain CE/PE volume, surfaced at the top level too —
        # doubles as the volume fallback (#3) when neither Angel One's index
        # candles nor its futures candles had usable volume data (see
        # data_fetcher._try_angel_historical's fallback chain).
        market_data["option_volume"] = {
            "total_ce_volume": oi_summary.get("total_ce_volume", 0),
            "total_pe_volume": oi_summary.get("total_pe_volume", 0),
            "volume_pcr":      oi_summary.get("volume_pcr", 0.0),
        }

        return _sanitize(market_data)

    async def _safe_historical(self, symbol: str) -> Dict:
        try:
            return await self.fetcher.get_historical_prices(symbol, days=75)
        except Exception as e:
            logger.warning(f"Historical fetch failed for {symbol}: {e}")
            return {"closes": [], "volumes": []}

    async def _safe_multi_timeframe(self, symbol: str) -> Dict:
        """
        Fetch one real 5-minute OHLCV stream from the broker and derive
        15-minute and 1-hour candles locally.

        Derived candles are timestamp/session aligned to the NSE 09:15
        trading-session anchor. Incomplete or non-contiguous source
        buckets are skipped so overnight/session gaps cannot create
        false 15-minute or 1-hour candles.
        """
        unavailable = {
            "5min": {"data_source": "unavailable", "trend": "unavailable"},
            "15min": {"data_source": "unavailable", "trend": "unavailable"},
            "1hr": {"data_source": "unavailable", "trend": "unavailable"},
        }

        try:
            res5 = await self.fetcher.get_intraday_ohlc(
                symbol, interval="FIVE_MINUTE", bars=1200
            )

            if not res5 or not res5.get("available"):
                return unavailable

            highs = res5.get("highs", [])
            lows = res5.get("lows", [])
            closes = res5.get("closes", [])
            volumes = res5.get("volumes", [])
            timestamps = res5.get("timestamps", [])

            n = min(len(highs), len(lows), len(closes), len(timestamps))
            if n <= 0:
                return unavailable

            volumes_ok = len(volumes) >= n
            rows = []

            for i in range(n):
                ts_raw = timestamps[i]
                try:
                    if not ts_raw:
                        continue

                    ts_text = str(ts_raw).strip()
                    if ts_text.endswith("Z"):
                        ts_text = ts_text[:-1] + "+00:00"

                    ts = datetime.fromisoformat(ts_text)
                except (TypeError, ValueError):
                    continue

                rows.append(
                    (
                        ts,
                        highs[i],
                        lows[i],
                        closes[i],
                        volumes[i] if volumes_ok else 0,
                    )
                )

            if not rows:
                return unavailable

            # Sort chronologically and remove duplicate timestamps.
            rows.sort(key=lambda x: x[0])

            deduped = []
            seen = set()
            for row in rows:
                ts = row[0]
                if ts in seen:
                    continue
                seen.add(ts)
                deduped.append(row)

            rows = deduped

            def build_session_bars(step: int):
                """
                Aggregate 5-minute candles into complete NSE-session
                buckets.

                step=3  -> 15-minute candles
                step=12 -> 1-hour candles

                Buckets are anchored at 09:15 rather than at arbitrary
                positions in the returned broker array.
                """
                if len(rows) < step:
                    return None

                grouped = {}

                for row in rows:
                    ts = row[0]
                    session_start = ts.replace(
                        hour=9, minute=15, second=0, microsecond=0
                    )

                    # Ignore pre-market/out-of-session candles.
                    if ts < session_start:
                        continue

                    # NSE regular session ends at 15:30. A candle starting
                    # at 15:30 or later is outside the regular session.
                    session_end = session_start.replace(
                        hour=15, minute=30
                    )
                    if ts >= session_end:
                        continue

                    elapsed_minutes = int(
                        (ts - session_start).total_seconds() // 60
                    )

                    # Ignore anything that does not land on a 5-minute
                    # candle boundary relative to the NSE 09:15 anchor.
                    if elapsed_minutes < 0 or elapsed_minutes % 5 != 0:
                        continue

                    bucket_index = elapsed_minutes // 5
                    bucket = bucket_index // step
                    key = (ts.date(), bucket)

                    grouped.setdefault(key, []).append(row)

                out_h, out_l, out_c, out_v, out_t = [], [], [], [], []

                for key in sorted(grouped):
                    bucket_rows = grouped[key]

                    # A valid derived candle must contain exactly the
                    # expected number of consecutive 5-minute candles.
                    if len(bucket_rows) != step:
                        continue

                    consecutive = True
                    for a, b in zip(bucket_rows, bucket_rows[1:]):
                        if b[0] - a[0] != timedelta(minutes=5):
                            consecutive = False
                            break

                    if not consecutive:
                        continue

                    out_h.append(max(r[1] for r in bucket_rows))
                    out_l.append(min(r[2] for r in bucket_rows))
                    out_c.append(bucket_rows[-1][3])
                    out_v.append(sum(r[4] for r in bucket_rows))
                    out_t.append(bucket_rows[-1][0].isoformat())

                if not out_c:
                    return None

                return {
                    "highs": out_h,
                    "lows": out_l,
                    "closes": out_c,
                    "volumes": out_v,
                    "timestamps": out_t,
                }

            # Keep the existing 5-minute frontend/API contract.
            recent_rows = rows[-100:]

            frames = {
                "5min": {
                    "highs": [r[1] for r in recent_rows],
                    "lows": [r[2] for r in recent_rows],
                    "closes": [r[3] for r in recent_rows],
                    "volumes": [r[4] for r in recent_rows],
                    "timestamps": [r[0].isoformat() for r in recent_rows],
                },
                "15min": build_session_bars(3),
                "1hr": build_session_bars(12),
            }

            out = {}

            for label, data in frames.items():
                if not data:
                    out[label] = {
                        "data_source": "unavailable",
                        "trend": "unavailable",
                    }
                    continue

                ind = self.tech.compute_from_ohlc(
                    data["highs"],
                    data["lows"],
                    data["closes"],
                    data.get("volumes"),
                )

                frame_closes = data["closes"]

                if len(frame_closes) >= 2 and ind.get("ema20", 0) > 0:
                    last = frame_closes[-1]

                    if (
                        last > ind["ema20"]
                        and ind.get("adx", 0) >= 15
                        and ind.get("di_plus", 0) > ind.get("di_minus", 0)
                    ):
                        trend = "up"
                    elif (
                        last < ind["ema20"]
                        and ind.get("adx", 0) >= 15
                        and ind.get("di_minus", 0) > ind.get("di_plus", 0)
                    ):
                        trend = "down"
                    else:
                        trend = "sideways"
                else:
                    trend = "sideways"

                out[label] = {
                    "data_source": res5.get(
                        "data_source", "angel_one_intraday"
                    ),
                    "trend": trend,
                    "indicators": ind,
                    "bar_count": len(frame_closes),
                    "last_close": frame_closes[-1] if frame_closes else 0,
                    "highs": data.get("highs", []),
                    "lows": data.get("lows", []),
                    "closes": data.get("closes", []),
                    "volumes": data.get("volumes", []),
                    "timestamps": data.get("timestamps", []),
                }

            return out

        except Exception as e:
            logger.warning(
                f"Multi-timeframe fetch failed for {symbol}: {e}"
            )
            return unavailable

    async def _safe_futures_premium(self, symbol: str) -> Dict:
        try:
            return await self.fetcher.get_futures_premium(symbol)
        except Exception as e:
            logger.warning(f"Futures premium fetch failed for {symbol}: {e}")
            return {"status": "unavailable", "premium": 0.0, "premium_pct": 0.0}

    async def _safe_oi_change_tracked(self, symbol: str, expiry: str, opt_df) -> Dict:
        try:
            return await history_service.get_oi_change_since(symbol, expiry, opt_df, minutes_ago=15)
        except Exception as e:
            logger.warning(f"Tracked OI-change lookup failed for {symbol}: {e}")
            return {"available": False, "reason": str(e)}

    async def _safe_option_chain(self, symbol: str, expiry: str = None) -> Dict:
        try:
            return await self.fetcher.get_option_chain(symbol, expiry=expiry)
        except Exception as e:
            logger.warning(f"Option chain unavailable for {symbol}; using spot/technical fallback: {e}")
            return {"symbol": symbol, "expiry": "", "all_expiries": [], "underlying_price": 0, "data": []}

    async def _safe_volatility(self) -> float:
        try:
            return await self.fetcher.get_volatility()
        except Exception as e:
            logger.warning(f"VIX fetch failed: {e}")
            return 0.0

    async def _safe_breadth(self) -> Dict:
        try:
            return await self.fetcher.get_market_breadth()
        except Exception as e:
            logger.warning(f"Market breadth fetch failed: {e}")
            return {"advances": 0, "declines": 0, "unchanged": 0, "source": "error"}

    async def _safe_global_market(self) -> Dict:
        try:
            return await global_market_service.get_snapshot()
        except Exception as e:
            logger.warning(f"Global market fetch failed: {e}")
            return {"instruments": {}, "global_change_pct": None, "gift_nifty_change_pct": None}

    async def _safe_fii_dii(self) -> Dict:
        try:
            return await self.fetcher.get_fii_dii()
        except Exception as e:
            logger.warning(f"FII/DII fetch failed: {e}")
            return {"date": None, "fii": None, "dii": None, "source": "unavailable"}

    async def close(self):
        if self._owns_fetcher:
            await self.fetcher.close()
