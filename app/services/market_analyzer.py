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
from app.services.angel_live_feed import angel_live_feed
from app.services.market_session_state import classify_market_session_state
from app.utils.helpers import safe_float, expiry_filter
from app.utils.cache import async_cache
from app.config import settings
from app.utils.helpers import now_ist as current_ist
from app.utils.helpers import now_utc_naive, is_market_hours_ist

logger = logging.getLogger(__name__)

# Startup prewarm should warm the overview cache without creating
# a historical MarketData snapshot. Normal live/collector calls keep
# snapshot persistence enabled.
_snapshot_persistence_enabled = __import__("contextvars").ContextVar(
    "snapshot_persistence_enabled",
    default=True,
)

# Bound live OptionData persistence without adding broker calls.
_OPTION_SNAPSHOT_MIN_INTERVAL_SECONDS = 60.0
_option_snapshot_persist_lock = asyncio.Lock()
_option_snapshot_last_persisted = {}


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

        # One overview request must not fan out several Angel One calls at
        # exactly the same time.  The broker throttle controls call spacing,
        # but the actual SDK calls happen outside that gate.  This lock keeps
        # the Angel-heavy portion of one market snapshot serialized while
        # leaving NSE/global enrichment concurrent.
        self._angel_overview_lock = asyncio.Lock()

    # LIVE_CALC_REFRESH_LAYER_20260923_V2
    #
    # Expensive historical / MTF / enrichment context remains TTL-cached.
    # Live-sensitive values are refreshed separately during market hours.
    async def get_full_market_overview(
        self,
        symbol: str,
        expiry: str = None,
        cache_bust: str = "",
    ) -> Dict:
        cached = await self._get_full_market_overview_cached(
            symbol,
            expiry=expiry,
            cache_bust=cache_bust,
        )

        if not is_market_hours_ist(current_ist()):
            return cached

        return await self._refresh_live_calculations(
            cached,
            symbol,
            expiry,
        )

    async def _refresh_live_calculations(
        self,
        cached: Dict,
        symbol: str,
        expiry: str = None,
    ) -> Dict:
        """
        Refresh live-sensitive calculations without rebuilding the expensive
        historical / MTF / global enrichment context.
        """
        try:
            from copy import deepcopy

            result = deepcopy(cached)
            sym = str(symbol or "").upper()

            # Bypass DataFetcher's 10-second get_spot cache.
            live_spot = await self.fetcher._try_angel_live_feed_spot(sym)

            if isinstance(live_spot, dict):
                spot = dict(result.get("spot") or {})

                # PRESERVE_VALID_PREV_CLOSE_20260925
                # WebSocket LTP may expose prev_close=0. Never let an
                # invalid live-feed value erase a valid cached value.
                cached_prev_close = safe_float(spot.get("prev_close", 0))
                cached_prev_close_source = spot.get("prev_close_source")

                spot.update(live_spot)

                live_prev_close = safe_float(live_spot.get("prev_close", 0))
                if live_prev_close <= 0 and cached_prev_close > 0:
                    spot["prev_close"] = cached_prev_close
                    if cached_prev_close_source:
                        spot["prev_close_source"] = cached_prev_close_source

                    live_price = safe_float(spot.get("price", 0))
                    if live_price > 0:
                        spot["change"] = round(
                            live_price - cached_prev_close, 2
                        )
                        spot["change_percent"] = round(
                            ((live_price - cached_prev_close) / cached_prev_close) * 100,
                            2,
                        )

                result["spot"] = spot
            else:
                spot = result.get("spot") or {}

            spot_price = float(spot.get("price") or 0.0)
            if spot_price <= 0:
                return cached

            # REST contract data remains cached; this public method overlays
            # the latest Angel WebSocket option ticks outside that cache.
            chain = await self.fetcher.get_option_chain(sym, expiry)

            if not isinstance(chain, dict) or not chain.get("data"):
                return cached

            opt_df = self.option.process_option_chain(chain)

            if opt_df.empty:
                return cached

            # Persist the already-fetched live chain at a bounded cadence.
            # No additional Angel One request is made here.
            if _snapshot_persistence_enabled.get() and is_market_hours_ist(current_ist()):
                snapshot_symbol = str(chain.get("symbol", sym) or sym).upper()
                snapshot_expiry = str(chain.get("expiry", expiry or "") or "")

                if snapshot_expiry:
                    snapshot_key = (snapshot_symbol, snapshot_expiry)
                    now_mono = asyncio.get_running_loop().time()

                    async with _option_snapshot_persist_lock:
                        last_saved = _option_snapshot_last_persisted.get(
                            snapshot_key, 0.0
                        )

                        if (
                            now_mono - last_saved
                            >= _OPTION_SNAPSHOT_MIN_INTERVAL_SECONDS
                        ):
                            saved = await save_option_chain_snapshot(
                                snapshot_symbol,
                                snapshot_expiry,
                                opt_df,
                            )

                            if saved:
                                _option_snapshot_last_persisted[
                                    snapshot_key
                                ] = now_mono

                                logger.info(
                                    "OPTION_SNAPSHOT_PERSISTED: %s %s rows=%d",
                                    snapshot_symbol,
                                    snapshot_expiry,
                                    len(opt_df) * 2,
                                )

            pcr = self.option.compute_pcr(opt_df)
            max_pain = self.option.compute_max_pain(opt_df)
            oi_change = self.option.compute_oi_change(opt_df)
            oi_summary = self.option.oi_summary(
                opt_df,
                underlying=spot_price,
            )

            result["spot"] = spot
            result["option_chain"] = chain
            result["option_chain_valid"] = True
            result["option_chain_rows"] = len(chain.get("data") or [])
            result["pcr"] = pcr
            result["max_pain"] = max_pain
            result["oi_change"] = oi_change
            result["oi_summary"] = oi_summary

            daily_sr = result.get("daily_support_resistance")

            if isinstance(daily_sr, dict):
                technicals = result.get("technicals") or {}
                vwap = float(technicals.get("vwap") or 0.0)

                result["support_resistance"] = (
                    self.tech.combine_support_resistance(
                        daily_sr,
                        oi_summary,
                        spot_price,
                        vwap=vwap,
                    )
                )

            result["timestamp"] = current_ist().isoformat()

            # LIVE_SESSION_STATE_REFRESH_20260924
            # `_refresh_live_calculations()` updates the live spot/option
            # snapshot without rebuilding the expensive MTF pipeline.
            # Recompute the session-state observation from the current
            # live spot plus the existing real MTF frame so phase/day-type/
            # transition information does not remain frozen at the original
            # overview-cache timestamp.
            try:
                result["session_state"] = classify_market_session_state(result)
            except Exception as exc:
                logger.warning(
                    "Live session-state refresh failed for %s: %s",
                    sym,
                    exc,
                )

            decision = run_decision_engine(result)
            result["decision"] = decision

            result["decision"]["strategy_detail"] = (
                generate_option_strategy(result, opt_df)
            )
            result["decision"]["price_levels"] = (
                generate_price_levels(result)
            )

            result["tamil_indicators"] = build_tamil_indicators(
                result,
                decision,
            )

            return result

        except Exception as exc:
            logger.warning(
                "LIVE_CALC_REFRESH failed for %s: %s",
                symbol,
                exc,
                exc_info=True,
            )
            return cached

    @async_cache(ttl=settings.analysis_cache_ttl)
    async def _get_full_market_overview_cached(
        self,
        symbol: str,
        expiry: str = None,
        cache_bust: str = "",
    ) -> Dict:
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
        # cache_bust is intentionally not used in the data pipeline.
        # async_cache includes function arguments in its key, so a unique
        # value forces a fresh overview only for an explicit manual refresh.
        _ = cache_bust

        # ── 0. Closed-market fast path ───────────────────────────────────
        #
        # IMPORTANT:
        # During NSE closed hours do NOT call Angel One / NSE live endpoints,
        # intraday OHLC, futures LTP, option-chain APIs, or enrichment APIs.
        # Reuse the last valid persisted trading-session snapshot instead.
        #
        # The normal live-market pipeline below remains unchanged.
        market_open_now = is_market_hours_ist(current_ist())

        if not market_open_now:
            cutoff_utc = now_utc_naive()

            logger.info(
                "CLOSED-MARKET FAST PATH: symbol=%s expiry=%s "
                "live broker/enrichment fetches skipped",
                symbol,
                expiry,
            )

            spot = await history_service.get_latest_market_snapshot_before(
                symbol,
                cutoff_utc,
            )

            if not spot:
                raise RuntimeError(
                    f"No valid persisted trading-session spot available "
                    f"for closed-market fallback: {symbol}"
                )

            # Explicitly mark this as closed/stale. Never let the persisted
            # snapshot's historical timestamp make it look like a live tick.
            spot = dict(spot)
            spot["market_open"] = False
            spot["market_status_source"] = "history_db_closed_fallback"
            spot["data_source"] = "history_db_closed_fallback"
            spot["stale"] = True

            chain = None

            if expiry:
                chain = (
                    await history_service
                    .get_latest_option_chain_snapshot_before(
                        symbol,
                        expiry,
                        cutoff_utc,
                    )
                )

            if not chain:
                chain = {
                    "symbol": symbol,
                    "expiry": expiry or "",
                    "all_expiries": [expiry] if expiry else [],
                    "underlying_price": safe_float(
                        spot.get("price", 0)
                    ),
                    "data": [],
                    "data_source": "history_db_closed_fallback",
                    "snapshot_timestamp": "",
                    "market_open": False,
                }
            else:
                chain = dict(chain)
                chain["underlying_price"] = safe_float(
                    spot.get("price", 0)
                )
                chain["market_open"] = False
                chain["stale"] = True

            # Do not run broker historical/intraday/futures calls while closed.
            # There is deliberately no attempt to manufacture OHLC indicators
            # from persisted spot snapshots.
            hist = {
                "closes": [],
                "volumes": [],
                "available": False,
                "data_source": "history_db_closed_fallback",
                "market_open": False,
                "reason": "Market closed; live historical fetch skipped",
            }

            # Closed-market MTF replay:
            # read the last persisted real 5-minute session and reuse the same
            # MTF aggregation/indicator calculation. No broker call is made.
            persisted_mtf_rows = (
                await history_service.get_latest_intraday_ohlc_before(
                    symbol,
                    cutoff_utc,
                    limit=1200,
                )
            )

            if persisted_mtf_rows:
                multi_tf = await self._safe_multi_timeframe(
                    symbol,
                    persisted_rows=persisted_mtf_rows,
                    closed_replay=True,
                )
            else:
                multi_tf = {
                    "5min": {
                        "available": False,
                        "fresh": False,
                        "data_source": "history_db_intraday_ohlc",
                        "market_open": False,
                        "historical_replay": True,
                    },
                    "15min": {
                        "available": False,
                        "fresh": False,
                        "data_source": "history_db_intraday_ohlc",
                        "market_open": False,
                        "historical_replay": True,
                    },
                    "1hr": {
                        "available": False,
                        "fresh": False,
                        "data_source": "history_db_intraday_ohlc",
                        "market_open": False,
                        "historical_replay": True,
                    },
                }

            futures_data = {
                "premium": 0.0,
                "premium_pct": 0.0,
                "status": "unavailable",
                "data_source": "history_db_closed_fallback",
                "market_open": False,
            }

            # Enrichment values are intentionally unavailable rather than
            # making fresh network calls while the exchange is closed.
            # decision_engine expects `vix` as a numeric float.
            # Closed market has no fresh VIX fetch, so use 0 as the
            # unavailable sentinel; _score_vix() handles vix <= 0 safely.
            vix = 0.0

            breadth = {
                "advance": 0,
                "decline": 0,
                "unchanged": 0,
                "ratio": 0.0,
                "status": "unavailable",
                "data_source": "closed_market",
            }

            global_snap = {
                "global_change_pct": None,
                "gift_nifty_change_pct": None,
                "instruments": {},
                "status": "unavailable",
                "data_source": "closed_market",
            }

            fii_dii = {
                "fii": {"net_value": None, "status": "unavailable"},
                "dii": {"net_value": None, "status": "unavailable"},
                "status": "unavailable",
                "data_source": "closed_market",
            }

            # Continue through the exact same processing/decision path below.
            # This is important: decision_engine already has the explicit
            # market_open=False hard gate → WAIT / preferred_side NONE.
        else:

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

                        # ── Angel pipeline + independent enrichment ────────────────────────
                        # The Angel throttle spaces broker calls, but SDK calls themselves run
                        # outside the throttle gate.  Starting spot/chain/history/MTF/futures
                        # together therefore creates a burst against the same Angel account
                        # and can trigger broker-side rate limiting and 8-9s retries.
                        #
                        # Keep independent NSE/global work concurrent, but serialize the
                        # Angel-heavy snapshot pipeline.  This preserves the complete analysis
                        # while preventing a cold request from creating an Angel call burst.
                        async def _angel_pipeline():
                            async with self._angel_overview_lock:
                                # MTF is the critical live-data dependency for the
                                # decision hard-gate. Fetch it first so a cold historical
                                # cache cannot delay the FIVE_MINUTE broker request.
                                multi_tf = await _timed(
                                    "multi_timeframe",
                                    self._safe_multi_timeframe(symbol),
                                )
                                spot = await _timed("spot", self.fetcher.get_spot(symbol))
                                chain = await _timed(
                                    "option_chain",
                                    self._safe_option_chain(symbol, expiry),
                                )
                                hist = await _timed(
                                    "historical",
                                    self._safe_historical(symbol),
                                )
                                futures_data = await _timed(
                                    "futures_premium",
                                    self._safe_futures_premium(symbol),
                                )
                                return spot, chain, hist, multi_tf, futures_data

                        angel_task = asyncio.create_task(_angel_pipeline())

                        vix_task = asyncio.create_task(
                            _timed("volatility", self._safe_volatility())
                        )
                        breadth_task = asyncio.create_task(
                            _timed("breadth", self._safe_breadth())
                        )
                        global_task = asyncio.create_task(
                            _timed("global_market", self._safe_global_market())
                        )
                        fii_task = asyncio.create_task(
                            _timed("fii_dii", self._safe_fii_dii())
                        )

                        (
                            (spot, chain, hist, multi_tf, futures_data),
                            vix,
                            breadth,
                            global_snap,
                            fii_dii,
                        ) = await asyncio.gather(
                            angel_task,
                            vix_task,
                            breadth_task,
                            global_task,
                            fii_task,
                        )

        # Fresh Angel WebSocket provides the live LTP but its LTP payload
        # does not contain previous-close/OHLC fields.  The daily historical
        # fetch above already gives us the previous trading-day close, so use
        # that value here instead of making another broker get_ltp() call.
        #
        # This preserves the low-latency WebSocket spot path while keeping
        # confluence/market-regime trend calculations supplied with a valid
        # previous close.
        spot = dict(spot or {})
        if safe_float(spot.get("prev_close", 0)) <= 0:
            _hist_closes = hist.get("closes") or []
            if _hist_closes:
                _prev_close = safe_float(_hist_closes[-1])
                _spot_price = safe_float(spot.get("price", 0))

                if _prev_close > 0:
                    spot["prev_close"] = _prev_close
                    spot["prev_close_source"] = "historical_daily_close"

                    if _spot_price > 0:
                        _change = _spot_price - _prev_close
                        spot["change"] = round(_change, 2)
                        spot["change_percent"] = round(
                            (_change / _prev_close) * 100, 2
                        )

                    logger.info(
                        "Backfilled previous close for %s from daily history: "
                        "price=%.2f prev_close=%.2f",
                        symbol,
                        _spot_price,
                        _prev_close,
                    )

        # Persist the already-fetched live/enriched spot on cache misses only.
        # Startup prewarm can disable this explicitly.
        # No extra Angel One call; cache hits skip this function entirely.
        if (
            market_open_now
            and _snapshot_persistence_enabled.get()
        ):
            await history_service.save_market_snapshot(spot)

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
        if market_open_now:
            await save_option_chain_snapshot(
                chain.get("symbol", symbol),
                expiry,
                opt_df,
            )

        # Our OWN OI-change-over-time, computed from snapshots this app has
        # actually saved (not the provider's single day-change field) — e.g.
        # "what changed in the last ~15 minutes", with buildup classification
        # (long/short buildup, long unwinding, short covering) per side.
        if market_open_now:
            oi_change_tracked = await self._safe_oi_change_tracked(
                chain.get("symbol", symbol), expiry, opt_df
            )
        else:
            oi_change_tracked = {
                "available": False,
                "status": "unavailable",
                "reason": "Market closed; live OI tracking skipped",
                "data_source": "closed_market",
            }

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

        # Canonical internal key remains "1hr" for DecisionEngine
        # compatibility; expose "1h" as the public/API alias as well.
        if isinstance(multi_tf, dict) and "1hr" in multi_tf:
            multi_tf.setdefault("1h", multi_tf["1hr"])

        market_data = {
            "symbol":        symbol,
            "spot":          spot,
            "option_chain":  chain,
            # Option chain validity flag — True only when data was actually
            # fetched (non-empty rows). Used by decision_engine's critical
            # data gate to block CALL/PUT signals when chain is unavailable.
            # PCR > 0 is the gate's primary check; this flag is for UI display.
            "option_chain_valid": bool(chain.get("data")),
            "option_chain_rows":  len(chain.get("data") or []),
            "vix":           vix,
            "breadth":       breadth,
            "pcr":           pcr,
            "max_pain":      max_pain,
            "oi_change":     oi_change,
            "oi_change_tracked": oi_change_tracked,
            "oi_summary":    oi_summary,
            "support_resistance": sr,
            "daily_support_resistance": daily_sr,
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
            "timestamp":     current_ist().isoformat(),
        }

        # MARKET_SESSION_STATE_STAGE1_20260924
        # Observation/context only. Does not modify DecisionEngine scoring,
        # lifecycle, entry gates, or strategy selection.
        try:
            market_data["session_state"] = classify_market_session_state(
                market_data
            )
        except Exception as exc:
            logger.warning(
                "Market session state calculation failed for %s: %s",
                symbol,
                exc,
            )
            market_data["session_state"] = {
                "phase": "UNKNOWN",
                "day_type": "UNKNOWN",
                "transition_state": "NONE",
                "state_confidence": 0,
                "observation_only": True,
                "reasons": ["Session-state calculation unavailable"],
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
            from app.services.angel_one import (
                ANGEL_PRIORITY_BACKGROUND,
                get_angel_call_priority,
            )

            include_volume_proxy = (
                get_angel_call_priority() == ANGEL_PRIORITY_BACKGROUND
            )

            return await self.fetcher.get_historical_prices(
                symbol,
                days=75,
                include_volume_proxy=include_volume_proxy,
            )
        except Exception as e:
            logger.warning(f"Historical fetch failed for {symbol}: {e}")
            return {"closes": [], "volumes": []}

    async def _safe_multi_timeframe(
        self,
        symbol: str,
        persisted_rows=None,
        closed_replay: bool = False,
    ) -> Dict:
        """
        Fetch one real 5-minute OHLCV stream from the broker and derive
        15-minute and 1-hour candles locally.
        """
        unavailable = {
            "5min": {"data_source": "unavailable", "trend": "unavailable"},
            "15min": {"data_source": "unavailable", "trend": "unavailable"},
            "1hr": {"data_source": "unavailable", "trend": "unavailable"},
        }

        try:
            open_by_timestamp = {}

            if persisted_rows is not None:
                # CLOSED-MARKET REPLAY:
                # Do not call any broker. Reuse persisted real 5-minute bars.
                rows = list(persisted_rows)

                res5 = {
                    "available": bool(rows),
                    "data_source": "history_db_intraday_ohlc",
                }

                if not rows:
                    return unavailable

            else:
                res5 = await self.fetcher.get_intraday_ohlc(
                    symbol,
                    interval="FIVE_MINUTE",
                    bars=1200,
                )

                # LIVE_DB_MTF_FALLBACK_20260924
                # Angel REST may be rate-limited even though real completed 5M Angel
                # candles are already persisted locally and/or available from WS.
                # Never treat a last-known-good broker snapshot as current live data.
                broker_snapshot_failed = (
                    not res5
                    or not res5.get("available")
                    or bool(res5.get("snapshot_fallback", False))
                )

                if broker_snapshot_failed:
                    broker_reason = (
                        res5.get("snapshot_fallback_reason")
                        if isinstance(res5, dict) else None
                    ) or (
                        res5.get("reason")
                        if isinstance(res5, dict) else None
                    )

                    try:
                        db_rows = await history_service.get_latest_intraday_ohlc_before(
                            symbol,
                            current_ist(),
                            limit=1200,
                            include_data_source=True,
                        )
                    except Exception as db_exc:
                        logger.warning(
                            "LIVE DB MTF fallback failed for %s: %s",
                            symbol,
                            db_exc,
                        )
                        db_rows = []

                    if not db_rows:
                        logger.warning(
                            "LIVE DB MTF fallback unavailable for %s; broker_reason=%s",
                            symbol,
                            broker_reason,
                        )
                        return unavailable

                    trusted_db_sources = {
                        "angel_one_intraday",
                        "zerodha_intraday",
                    }
                    db_sources = {
                        str(r[5]).strip()
                        for r in db_rows
                        if len(r) > 5 and r[5]
                    }
                    db_broker_lineage = (
                        bool(db_sources)
                        and len(db_sources) == 1
                        and db_sources.issubset(trusted_db_sources)
                        and all(len(r) > 5 and r[5] for r in db_rows)
                    )
                    db_data_source = (
                        next(iter(db_sources))
                        if db_broker_lineage
                        else "history_db_intraday_ohlc"
                    )

                    res5 = {
                        "available": True,
                        "data_source": db_data_source,
                        "db_snapshot_fallback": True,
                        "db_broker_lineage": db_broker_lineage,
                        "snapshot_fallback_reason": broker_reason,
                    }

                    highs = [r[1] for r in db_rows]
                    lows = [r[2] for r in db_rows]
                    closes = [r[3] for r in db_rows]
                    volumes = [r[4] for r in db_rows]
                    opens = []
                    timestamps = [r[0].isoformat() for r in db_rows]

                    logger.info(
                        "LIVE DB MTF FALLBACK: %s rows=%d source=%s "
                        "broker_lineage=%s",
                        str(symbol).upper(),
                        len(db_rows),
                        db_data_source,
                        db_broker_lineage,
                    )

                    # DB_FALLBACK_FUTURES_VOLUME_ENRICH_20260924
                    # Persisted NIFTY index candles intentionally carry volume=0.
                    # Reuse the existing short-lived futures-volume proxy cache so
                    # DB fallback does not destroy VWAP/volume indicators.
                    if not any(volumes):
                        cached_futures_volumes = (
                            self.fetcher._get_cached_futures_volumes(
                                symbol,
                                timestamps,
                            )
                        )

                        if cached_futures_volumes is not None:
                            volumes = cached_futures_volumes

                            logger.info(
                                "Applied cached %s futures volume to DB MTF fallback — %d bars",
                                str(symbol).upper(),
                                sum(1 for v in cached_futures_volumes if int(v or 0) > 0),
                            )

                        else:
                            now_ist = current_ist()
                            from_str = (
                                now_ist - timedelta(days=30)
                            ).strftime("%Y-%m-%d 09:15")
                            to_str = now_ist.strftime("%Y-%m-%d %H:%M")

                            self.fetcher._schedule_futures_volume_refresh(
                                symbol,
                                "FIVE_MINUTE",
                                from_str,
                                to_str,
                            )

                            logger.info(
                                "Scheduled futures volume proxy refresh for DB MTF fallback: %s/FIVE_MINUTE",
                                str(symbol).upper(),
                            )

                    logger.info(
                        "LIVE DB MTF FALLBACK: %s rows=%d latest=%s",
                        symbol,
                        len(db_rows),
                        timestamps[-1] if timestamps else None,
                    )
                else:
                    highs = res5.get("highs", [])
                    lows = res5.get("lows", [])
                    closes = res5.get("closes", [])
                    volumes = res5.get("volumes", [])
                    opens = res5.get("opens", [])
                    timestamps = res5.get("timestamps", [])

                n = min(
                    len(highs),
                    len(lows),
                    len(closes),
                    len(timestamps),
                )

                if n <= 0:
                    return unavailable

                volumes_ok = len(volumes) >= n
                opens_ok = len(opens) >= n
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

                    if ts.tzinfo is None:
                        from zoneinfo import ZoneInfo
                        ts = ts.replace(
                            tzinfo=ZoneInfo("Asia/Kolkata")
                        )

                    rows.append(
                        (
                            ts,
                            highs[i],
                            lows[i],
                            closes[i],
                            volumes[i] if volumes_ok else 0,
                        )
                    )

                    if opens_ok:
                        try:
                            open_by_timestamp[ts] = float(opens[i])
                        except (TypeError, ValueError):
                            pass

                if not rows:
                    return unavailable

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

            # WS_5M_MERGE_20260923
            # Merge completed local Angel WebSocket 5-minute candles.
            # Current/incomplete candle is intentionally excluded.
            try:
                ws_candles = angel_live_feed.get_completed_5m_candles(symbol)

                if persisted_rows is None and ws_candles:
                    from datetime import timezone
                    from zoneinfo import ZoneInfo

                    ws_ist = ZoneInfo("Asia/Kolkata")
                    merged_by_ts = {}

                    for row in rows:
                        ts = row[0]

                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=ws_ist)
                        else:
                            ts = ts.astimezone(ws_ist)

                        merged_by_ts[ts] = (
                            ts,
                            row[1],
                            row[2],
                            row[3],
                            row[4],
                        )

                    for candle in ws_candles:
                        ts_epoch = candle.get("timestamp")

                        if not isinstance(ts_epoch, (int, float)):
                            continue

                        ts = datetime.fromtimestamp(
                            float(ts_epoch),
                            tz=timezone.utc,
                        ).astimezone(ws_ist)

                        merged_by_ts[ts] = (
                            ts,
                            candle.get("high"),
                            candle.get("low"),
                            candle.get("close"),
                            candle.get("volume", 0.0),
                        )

                    rows = sorted(
                        merged_by_ts.values(),
                        key=lambda x: x[0],
                    )

                    logger.info(
                        "WS 5M MERGE: %s REST=%d WS_COMPLETED=%d MERGED=%d",
                        symbol,
                        len(deduped),
                        len(ws_candles),
                        len(rows),
                    )

            except Exception as ws_merge_exc:
                logger.warning(
                    "WS 5M MERGE skipped for %s: %s",
                    symbol,
                    ws_merge_exc,
                )

            # Persist the completed real 5-minute stream only in live mode.
            # Closed-market replay must never write synthetic/stale data back.
            # DB_MTF_LINEAGE_FIX_20260930
            # Never persist history-DB fallback as a live snapshot.
            if (
                persisted_rows is None
                and rows
                and not res5.get("db_snapshot_fallback")
            ):
                try:
                    await history_service.save_intraday_ohlc_snapshot(
                        symbol,
                        rows,
                        data_source=res5.get(
                            "data_source",
                            "angel_one_intraday",
                        ),
                        open_by_timestamp=open_by_timestamp,
                    )
                except Exception as persist_exc:
                    logger.warning(
                        "MTF 5M persistence skipped for %s: %s",
                        symbol,
                        persist_exc,
                    )

            def build_session_bars(step: int):
                """
                Aggregate complete 5-minute NSE-session buckets.

                step=3  -> 15-minute
                step=12 -> 1-hour
                """
                if len(rows) < step:
                    return None

                grouped = {}

                for row in rows:
                    ts = row[0]

                    session_start = ts.replace(
                        hour=9,
                        minute=15,
                        second=0,
                        microsecond=0,
                    )

                    if ts < session_start:
                        continue

                    session_end = session_start.replace(
                        hour=15,
                        minute=30,
                    )

                    if ts >= session_end:
                        continue

                    elapsed_minutes = int(
                        (ts - session_start).total_seconds() // 60
                    )

                    if elapsed_minutes < 0 or elapsed_minutes % 5 != 0:
                        continue

                    bucket_index = elapsed_minutes // 5
                    bucket = bucket_index // step
                    key = (ts.date(), bucket)

                    grouped.setdefault(key, []).append(row)

                out_h = []
                out_l = []
                out_c = []
                out_v = []
                out_t = []

                for key in sorted(grouped):
                    bucket_rows = grouped[key]

                    if len(bucket_rows) != step:
                        continue

                    consecutive = True

                    for a, b in zip(
                        bucket_rows,
                        bucket_rows[1:],
                    ):
                        if b[0] - a[0] != timedelta(minutes=5):
                            consecutive = False
                            break

                    if not consecutive:
                        continue

                    out_h.append(
                        max(r[1] for r in bucket_rows)
                    )
                    out_l.append(
                        min(r[2] for r in bucket_rows)
                    )
                    out_c.append(
                        bucket_rows[-1][3]
                    )
                    out_v.append(
                        sum(r[4] for r in bucket_rows)
                    )
                    out_t.append(
                        bucket_rows[-1][0].isoformat()
                    )

                if not out_c:
                    return None

                return {
                    "highs": out_h,
                    "lows": out_l,
                    "closes": out_c,
                    "volumes": out_v,
                    "timestamps": out_t,
                }

            # Use only the latest trading session for 5-minute indicators.
            # Do not mix the tail of a previous session into today's 5-minute frame.
            latest_session_date = rows[-1][0].date()

            session_rows = [
                r for r in rows
                if r[0].date() == latest_session_date
            ]

            recent_rows = session_rows[-100:]

            frames = {
                "5min": {
                    "highs": [r[1] for r in recent_rows],
                    "lows": [r[2] for r in recent_rows],
                    "closes": [r[3] for r in recent_rows],
                    "volumes": [r[4] for r in recent_rows],
                    "timestamps": [
                        r[0].isoformat()
                        for r in recent_rows
                    ],
                },
                "15min": build_session_bars(3),
                "1hr": build_session_bars(12),
            }

            # ── MTF freshness validation ─────────────────────────────────────
            # SAFETY FIX 2026-09-11:
            # 15m/1h indicators still use their full historical lookback.
            # We DO NOT reduce them to today's bars because EMA50/MACD/ADX
            # need sufficient history.
            #
            # Instead, we separately verify that the LAST candle belongs to
            # the current NSE session and is not excessively stale.
            #
            # Freshness limits are intentionally timeframe-aware:
            #   5min  -> 15 minutes
            #   15min -> 30 minutes
            #   1hr   -> 90 minutes
            #
            # This prevents an old broker response from being labelled
            # "live intraday" merely because its data_source says
            # "angel_one_intraday".
            from zoneinfo import ZoneInfo

            ist = ZoneInfo("Asia/Kolkata")
            now_ist = current_ist()

            # During NSE regular hours, every directional MTF frame must
            # belong to today's session and remain reasonably fresh.
            market_start = now_ist.replace(
                hour=9,
                minute=15,
                second=0,
                microsecond=0,
            )
            market_end = now_ist.replace(
                hour=15,
                minute=30,
                second=0,
                microsecond=0,
            )
            is_weekday = now_ist.weekday() < 5
            nse_market_open = (
                is_weekday
                and market_start <= now_ist < market_end
            )

            freshness_limits = {
                "5min": 15,
                "15min": 30,
                "1hr": 90,
            }

            out = {}

            for label, data in frames.items():
                if not data:
                    out[label] = {
                        "data_source": "unavailable",
                        "trend": "unavailable",
                        "fresh": False,
                        "freshness_reason": "No candle data",
                    }
                    continue

                # Use historical warm-up for 5m EMA50 and MACD.
                # Trend and freshness checks below still use the current session frame.
                if label == "5min" and len(rows) >= 50:
                    warmup_rows = rows[-200:]
                    indicator_highs = [r[1] for r in warmup_rows]
                    indicator_lows = [r[2] for r in warmup_rows]
                    indicator_closes = [r[3] for r in warmup_rows]
                    indicator_volumes = [r[4] for r in warmup_rows]
                else:
                    indicator_highs = data["highs"]
                    indicator_lows = data["lows"]
                    indicator_closes = data["closes"]
                    indicator_volumes = data.get("volumes")

                ind = self.tech.compute_from_ohlc(
                    indicator_highs,
                    indicator_lows,
                    indicator_closes,
                    indicator_volumes,
                    session_data=data if label == "5min" else None,
                )

                frame_closes = data["closes"]
                timestamps = data.get("timestamps", [])

                ema20 = ind.get("ema20", 0) or 0
                adx = ind.get("adx", 0) or 0
                di_plus = ind.get("di_plus", 0) or 0
                di_minus = ind.get("di_minus", 0) or 0

                if len(frame_closes) < 2 or ema20 <= 0:
                    trend = "unavailable"
                else:
                    last = frame_closes[-1]

                    if (
                        last > ema20
                        and adx >= 15
                        and di_plus > di_minus
                    ):
                        trend = "up"
                    elif (
                        last < ema20
                        and adx >= 15
                        and di_minus > di_plus
                    ):
                        trend = "down"
                    else:
                        trend = "sideways"

                # ── Determine the timestamp of the latest completed frame ──
                last_timestamp = None
                last_timestamp_ist = None
                freshness_minutes = None
                fresh = False
                freshness_reason = "Latest timestamp unavailable"

                if timestamps:
                    try:
                        ts_text = str(timestamps[-1]).strip()

                        if ts_text.endswith("Z"):
                            ts_text = ts_text[:-1] + "+00:00"

                        parsed_ts = datetime.fromisoformat(ts_text)

                        # Broker timestamps may occasionally arrive without
                        # timezone information. In that case the Angel/NSE
                        # intraday API convention is treated as IST.
                        if parsed_ts.tzinfo is None:
                            parsed_ts = parsed_ts.replace(tzinfo=ist)
                        else:
                            parsed_ts = parsed_ts.astimezone(ist)

                        last_timestamp_ist = parsed_ts
                        last_timestamp = parsed_ts.isoformat()

                        age_seconds = (
                            now_ist - parsed_ts
                        ).total_seconds()
                        freshness_minutes = round(
                            max(age_seconds, 0) / 60.0,
                            1,
                        )

                        max_age = freshness_limits.get(label, 30)

                        # DB_MTF_LINEAGE_FIX_20260930
                        # History DB fallback is never considered live-fresh.
                        if (
                            res5.get("db_snapshot_fallback")
                            and not res5.get("db_broker_lineage")
                        ):
                            fresh = False
                            freshness_reason = (
                                "History DB fallback; live broker "
                                "intraday snapshot unavailable"
                            )
                        elif closed_replay:
                            fresh = False
                            freshness_reason = (
                                "Closed market; persisted completed "
                                "session candle retained for historical MTF"
                            )
                        elif nse_market_open:
                            # During live NSE hours, today's session is
                            # mandatory for a directional MTF signal.
                            if parsed_ts.date() != now_ist.date():
                                fresh = False
                                freshness_reason = (
                                    "Latest candle is from an older session"
                                )
                            elif age_seconds < 0:
                                fresh = False
                                freshness_reason = (
                                    "Latest candle timestamp is in the future"
                                )
                            elif freshness_minutes <= max_age:
                                fresh = True
                                freshness_reason = (
                                    f"Fresh ({freshness_minutes:.1f}m old; "
                                    f"limit {max_age}m)"
                                )
                            else:
                                fresh = False
                                freshness_reason = (
                                    f"Stale ({freshness_minutes:.1f}m old; "
                                    f"limit {max_age}m)"
                                )
                        else:
                            # Outside regular NSE hours we don't force a
                            # same-day candle. The existing market_open /
                            # lifecycle safety gates handle non-trading time.
                            #
                            # Still expose timestamp metadata so diagnostics
                            # can see exactly which candle was used.
                            fresh = True
                            freshness_reason = (
                                "Outside NSE regular hours; "
                                "timestamp retained for diagnostics"
                            )

                    except (TypeError, ValueError):
                        freshness_reason = (
                            "Latest timestamp could not be parsed"
                        )

                out[label] = {
                    "data_source": res5.get(
                        "data_source",
                        "angel_one_intraday",
                    ),
                    "trend": trend,
                    "indicators": ind,
                    "bar_count": len(frame_closes),
                    "last_close": (
                        frame_closes[-1]
                        if frame_closes
                        else 0
                    ),
                    "highs": data.get("highs", []),
                    "lows": data.get("lows", []),
                    "closes": data.get("closes", []),
                    "volumes": data.get("volumes", []),
                    "timestamps": timestamps,

                    # SAFETY FIX 2026-09-11:
                    # These fields let DecisionEngine distinguish a real
                    # broker source from a stale broker response.
                    "last_timestamp": last_timestamp,
                    "session_date": (
                        last_timestamp_ist.date().isoformat()
                        if last_timestamp_ist
                        else None
                    ),
                    "fresh": fresh,
                    "freshness_minutes": freshness_minutes,
                    "freshness_reason": freshness_reason,
                    "market_open": not closed_replay,
                    "historical_replay": closed_replay,
                }

            logger.info(
                "MTF OBSERVE: %s market_open=%s replay=%s | "
                "5m fresh=%s age=%s source=%s reason=%s | "
                "15m fresh=%s age=%s source=%s reason=%s | "
                "1hr fresh=%s age=%s source=%s reason=%s",
                str(symbol).upper(),
                nse_market_open,
                closed_replay,
                out.get("5min", {}).get("fresh"),
                out.get("5min", {}).get("freshness_minutes"),
                out.get("5min", {}).get("data_source"),
                out.get("5min", {}).get("freshness_reason"),
                out.get("15min", {}).get("fresh"),
                out.get("15min", {}).get("freshness_minutes"),
                out.get("15min", {}).get("data_source"),
                out.get("15min", {}).get("freshness_reason"),
                out.get("1hr", {}).get("fresh"),
                out.get("1hr", {}).get("freshness_minutes"),
                out.get("1hr", {}).get("data_source"),
                out.get("1hr", {}).get("freshness_reason"),
            )

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
