"""
Technical Indicators — EMA, RSI, MACD, ADX, ATR, Supertrend, VWAP, Volume
"""
import numpy as np
import pandas as pd
from typing import List, Dict, Tuple


class TechnicalIndicators:

    @staticmethod
    def compute_ema(prices: List[float], period: int) -> float:
        if len(prices) < period:
            return 0.0
        s = pd.Series(prices)
        return float(s.ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def compute_all(prices: List[float], volumes: List[float] = None) -> Dict:
        """
        prices: daily close list, oldest first.
        volumes: daily volume list (same length), optional.
        Returns: ema20, ema50, rsi, macd dict, adx, di_plus, di_minus,
                 atr, supertrend, vwap, volume_spike, volume_ratio.
        """
        if not prices or len(prices) < 10:
            return TechnicalIndicators._empty()

        s = pd.Series(prices)

        # EMA
        ema20 = float(s.ewm(span=20, adjust=False).mean().iloc[-1]) if len(prices) >= 20 else 0.0
        ema50 = float(s.ewm(span=50, adjust=False).mean().iloc[-1]) if len(prices) >= 50 else 0.0

        # RSI
        rsi = TechnicalIndicators._rsi(prices)

        # MACD (12,26,9)
        macd = TechnicalIndicators._macd(prices)

        # ADX (needs high/low — approximate from close series)
        adx_data = TechnicalIndicators._adx_approx(prices)

        # ATR (approximate from close)
        atr = TechnicalIndicators._atr_approx(prices)

        # Supertrend (approximate)
        supertrend = TechnicalIndicators._supertrend_approx(prices, atr)

        # VWAP (approximate — needs intraday, use close*vol weighted)
        # ⚠️ NOTE: இது volumes list பாஸ் ஆனா மட்டும் வேலை செய்யும். இப்போதைக்கு
        # market_analyzer.py-ன் _safe_historical() எப்போதும் volumes=[] தான்
        # கொடுக்குது (data_fetcher.py-ல் historical volume support இல்லை),
        # அதனால் VWAP + volume_spike எப்போதும் 0/False-ஆ இருக்கும்.
        # இதை முழுசா fix பண்ண data_fetcher.py தேவை.
        vwap = 0.0
        volume_spike = False
        volume_ratio = 1.0
        if volumes and len(volumes) == len(prices):
            recent_v = volumes[-20:]
            avg_vol = np.mean(recent_v[:-1]) if len(recent_v) > 1 else recent_v[-1]
            last_vol = volumes[-1]
            volume_ratio = float(last_vol / avg_vol) if avg_vol > 0 else 1.0
            volume_spike = bool(volume_ratio > 1.5)
            # VWAP = sum(close*vol) / sum(vol) over last 20 bars
            recent_p = prices[-20:]
            if sum(recent_v) > 0:
                vwap = sum(p * v for p, v in zip(recent_p, recent_v)) / sum(recent_v)

        return {
            "ema20":        ema20,
            "ema50":        ema50,
            "rsi":          rsi,
            "macd":         macd,
            "adx":          adx_data["adx"],
            "di_plus":      adx_data["di_plus"],
            "di_minus":     adx_data["di_minus"],
            "atr":          atr,
            "supertrend":   supertrend,
            "vwap":         vwap,
            "volume_spike": volume_spike,
            "volume_ratio": volume_ratio,
        }

    @staticmethod
    def _rsi(prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(prices[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = gains.mean()
        avg_loss = losses.mean()
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    @staticmethod
    def _macd(prices: List[float]) -> Dict:
        if len(prices) < 35:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
        s = pd.Series(prices)
        ema12 = s.ewm(span=12, adjust=False).mean()
        ema26 = s.ewm(span=26, adjust=False).mean()
        line  = ema12 - ema26
        sig   = line.ewm(span=9, adjust=False).mean()
        hist  = line - sig
        return {
            "macd":      round(float(line.iloc[-1]), 2),
            "signal":    round(float(sig.iloc[-1]),  2),
            "histogram": round(float(hist.iloc[-1]), 2),
        }

    @staticmethod
    def _adx_approx(prices: List[float], period: int = 14) -> Dict:
        """
        Approximate ADX using close-only series (no OHLC).

        FIX: முந்தைய version ஒவ்வொரு i-க்கும் rolling mean-ஐ மறுபடியும்
        மறுபடியும் recompute பண்ணிச்சு (O(n²), confusing, NaN edge-cases
        சரியா handle ஆகல). இது ஒரே தடவை vectorized-ஆ rolling series
        கணக்கிட்டு, DX-ன் rolling mean-ஐ ADX-ஆ எடுக்கும் — standard
        Wilder-style approximation, close-only data-க்கு.
        """
        if len(prices) < period * 2:
            return {"adx": 0.0, "di_plus": 0.0, "di_minus": 0.0}

        p = np.array(prices, dtype=float)
        diffs = np.diff(p)
        tr = np.abs(diffs)  # simplified true range (no H/L)
        dm_plus  = np.where(diffs > 0, diffs, 0.0)
        dm_minus = np.where(diffs < 0, -diffs, 0.0)

        atr_series      = pd.Series(tr).rolling(period).mean()
        dm_plus_series  = pd.Series(dm_plus).rolling(period).mean()
        dm_minus_series = pd.Series(dm_minus).rolling(period).mean()

        atr_safe = atr_series.replace(0, np.nan)
        di_plus_series  = (dm_plus_series  / atr_safe) * 100
        di_minus_series = (dm_minus_series / atr_safe) * 100

        di_sum_safe = (di_plus_series + di_minus_series).replace(0, np.nan)
        dx_series  = ((di_plus_series - di_minus_series).abs() / di_sum_safe) * 100
        adx_series = dx_series.rolling(period).mean()

        def _last_or_zero(s: pd.Series) -> float:
            v = s.iloc[-1]
            return 0.0 if pd.isna(v) else float(v)

        return {
            "adx":      round(_last_or_zero(adx_series), 1),
            "di_plus":  round(_last_or_zero(di_plus_series), 1),
            "di_minus": round(_last_or_zero(di_minus_series), 1),
        }

    @staticmethod
    def _atr_approx(prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 0.0
        tr = np.abs(np.diff(prices[-(period + 1):]))
        return round(float(np.mean(tr)), 2)

    @staticmethod
    def _supertrend_approx(prices: List[float], atr: float, multiplier: float = 3.0) -> str:
        """Simplified Supertrend using close + ATR (no OHLC)."""
        if len(prices) < 10 or atr == 0:
            return ""
        current = prices[-1]
        prev    = prices[-2]
        mid     = (current + prev) / 2
        upper   = mid + multiplier * atr
        lower   = mid - multiplier * atr
        if current > upper:
            return "buy"
        elif current < lower:
            return "sell"
        return "neutral"

    @staticmethod
    def compute_from_ohlc(highs: List[float], lows: List[float], closes: List[float],
                           volumes: List[float] = None, period: int = 14) -> Dict:
        """
        TRUE OHLC-based indicators — Wilder's ADX/ATR (uses High/Low/Close,
        not close-only approximation) and a real Supertrend, plus a proper
        session VWAP when volumes are supplied. Use this whenever real
        intraday candles are available (see DataFetcher.get_intraday_ohlc /
        MarketAnalyzer._safe_multi_timeframe); compute_all() above stays as
        the close-only fallback for when only daily closes are available.
        """
        n = len(closes)
        if n < period + 2 or len(highs) != n or len(lows) != n:
            return TechnicalIndicators._empty()

        h = np.array(highs, dtype=float)
        l = np.array(lows, dtype=float)
        c = np.array(closes, dtype=float)

        # ── True Range & Wilder ATR ───────────────────────────────────────
        prev_close = np.roll(c, 1)
        prev_close[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_close), np.abs(l - prev_close)))
        atr_series = pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean()
        atr = float(atr_series.iloc[-1])

        # ── Wilder +DM/-DM → +DI/-DI → DX → ADX ───────────────────────────
        up_move   = h[1:] - h[:-1]
        down_move = l[:-1] - l[1:]
        plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        plus_dm_s  = pd.Series(plus_dm).ewm(alpha=1 / period, adjust=False).mean()
        minus_dm_s = pd.Series(minus_dm).ewm(alpha=1 / period, adjust=False).mean()
        atr_for_di = atr_series.iloc[1:].reset_index(drop=True).replace(0, np.nan)
        plus_di  = (plus_dm_s / atr_for_di) * 100
        minus_di = (minus_dm_s / atr_for_di) * 100
        di_sum   = (plus_di + minus_di).replace(0, np.nan)
        dx = ((plus_di - minus_di).abs() / di_sum) * 100
        adx_series = dx.ewm(alpha=1 / period, adjust=False).mean()

        def _last(s, default=0.0):
            if len(s) == 0:
                return default
            v = s.iloc[-1]
            return default if pd.isna(v) else float(v)

        adx      = round(_last(adx_series), 1)
        di_plus  = round(_last(plus_di), 1)
        di_minus = round(_last(minus_di), 1)

        # ── EMA / RSI / MACD on close ──────────────────────────────────────
        s = pd.Series(closes)
        ema20 = float(s.ewm(span=20, adjust=False).mean().iloc[-1]) if n >= 20 else 0.0
        ema50 = float(s.ewm(span=50, adjust=False).mean().iloc[-1]) if n >= 50 else 0.0
        rsi   = TechnicalIndicators._rsi(closes)
        macd  = TechnicalIndicators._macd(closes)

        # ── Real Supertrend (High/Low/Close + Wilder ATR, standard formula) ─
        supertrend, st_value = TechnicalIndicators._supertrend_ohlc(h, l, c, atr_series.values)

        # ── Session VWAP (typical price weighted by real volume) ───────────
        vwap = 0.0
        volume_spike = False
        volume_ratio = 1.0
        if volumes and len(volumes) == n and sum(volumes) > 0:
            typical = (h + l + c) / 3.0
            v = np.array(volumes, dtype=float)
            vwap = float(np.sum(typical * v) / np.sum(v))
            recent_v = v[-20:]
            avg_vol = np.mean(recent_v[:-1]) if len(recent_v) > 1 else recent_v[-1]
            last_vol = v[-1]
            volume_ratio = float(last_vol / avg_vol) if avg_vol > 0 else 1.0
            volume_spike = volume_ratio > 1.5

        return {
            "ema20": round(ema20, 2), "ema50": round(ema50, 2), "rsi": rsi, "macd": macd,
            "adx": adx, "di_plus": di_plus, "di_minus": di_minus,
            "atr": round(atr, 2), "supertrend": supertrend, "supertrend_value": round(st_value, 2),
            "vwap": round(vwap, 2), "volume_spike": bool(volume_spike), "volume_ratio": round(volume_ratio, 2),
            "source": "ohlc_wilder",
        }

    @staticmethod
    def _supertrend_ohlc(h: np.ndarray, l: np.ndarray, c: np.ndarray,
                          atr: np.ndarray, multiplier: float = 3.0) -> Tuple[str, float]:
        """Standard Supertrend recurrence on real High/Low/Close + Wilder ATR
        (not the close-only approximation in _supertrend_approx)."""
        n = len(c)
        if n < 3:
            return "", 0.0
        hl2 = (h + l) / 2.0
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        final_upper = np.zeros(n)
        final_lower = np.zeros(n)
        trend = np.ones(n, dtype=int)  # 1 = uptrend, -1 = downtrend
        final_upper[0] = basic_upper[0]
        final_lower[0] = basic_lower[0]

        for i in range(1, n):
            final_upper[i] = (
                basic_upper[i] if (basic_upper[i] < final_upper[i - 1] or c[i - 1] > final_upper[i - 1])
                else final_upper[i - 1]
            )
            final_lower[i] = (
                basic_lower[i] if (basic_lower[i] > final_lower[i - 1] or c[i - 1] < final_lower[i - 1])
                else final_lower[i - 1]
            )
            if trend[i - 1] == 1:
                trend[i] = -1 if c[i] < final_lower[i] else 1
            else:
                trend[i] = 1 if c[i] > final_upper[i] else -1

        last_trend = trend[-1]
        st_value = final_lower[-1] if last_trend == 1 else final_upper[-1]
        return ("buy" if last_trend == 1 else "sell"), float(st_value)

    @staticmethod
    def combine_support_resistance(pivot_sr: Dict, oi_summary: Dict, spot: float,
                                    vwap: float = 0.0) -> Dict:
        """
        Real Support/Resistance = combine multiple independent sources
        instead of a single pivot/placeholder guess:
          Support:    Put Max-OI strike, Pivot S1, VWAP (when below spot)
          Resistance: Call Max-OI strike, Pivot R1, VWAP (when above spot)
        Returns 5-level ladder: strong_support / support / pivot /
        resistance / strong_resistance, plus flat support[]/resistance[]
        lists (kept for backward compatibility with existing callers).
        """
        candidates_support: List[float] = []
        candidates_resistance: List[float] = []

        pivot_support = pivot_sr.get("support", [])
        pivot_resistance = pivot_sr.get("resistance", [])
        candidates_support.extend([v for v in pivot_support if v and v > 0])
        candidates_resistance.extend([v for v in pivot_resistance if v and v > 0])

        pe_strike = oi_summary.get("pe_max_oi_strike", 0)
        ce_strike = oi_summary.get("ce_max_oi_strike", 0)
        if pe_strike and pe_strike > 0:
            candidates_support.append(float(pe_strike))
        if ce_strike and ce_strike > 0:
            candidates_resistance.append(float(ce_strike))

        if vwap and vwap > 0:
            if vwap <= spot:
                candidates_support.append(float(vwap))
            else:
                candidates_resistance.append(float(vwap))

        candidates_support = sorted(set(round(v, 2) for v in candidates_support if v < spot), reverse=True)
        candidates_resistance = sorted(set(round(v, 2) for v in candidates_resistance if v > spot))

        if not candidates_support:
            candidates_support = [round(spot * 0.99, 2), round(spot * 0.98, 2)]
        if not candidates_resistance:
            candidates_resistance = [round(spot * 1.01, 2), round(spot * 1.02, 2)]

        result = {
            "support":    candidates_support[:3],
            "resistance": candidates_resistance[:3],
            "pivot":      pivot_sr.get("pivot", round(spot, 2)),
            "strong_support":    candidates_support[0] if candidates_support else round(spot * 0.98, 2),
            "support_level":     candidates_support[1] if len(candidates_support) > 1 else (candidates_support[0] if candidates_support else round(spot * 0.99, 2)),
            "resistance_level":  candidates_resistance[0] if candidates_resistance else round(spot * 1.01, 2),
            "strong_resistance": candidates_resistance[1] if len(candidates_resistance) > 1 else (candidates_resistance[0] if candidates_resistance else round(spot * 1.02, 2)),
            "sources": {
                "pivot": bool(pivot_support or pivot_resistance),
                "oi_walls": bool(pe_strike or ce_strike),
                "vwap": bool(vwap and vwap > 0),
            },
        }
        return result

    @staticmethod
    def _empty() -> Dict:
        return {
            "ema20": 0.0, "ema50": 0.0, "rsi": 50.0,
            "macd": {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
            "adx": 0.0, "di_plus": 0.0, "di_minus": 0.0,
            "atr": 0.0, "supertrend": "", "vwap": 0.0,
            "volume_spike": False, "volume_ratio": 1.0,
        }

    # Legacy compat
    @staticmethod
    def compute_support_resistance(price: float, **_) -> Dict:
        return {
            "support":    [round(price * 0.98, 2), round(price * 0.95, 2)],
            "resistance": [round(price * 1.02, 2), round(price * 1.05, 2)],
        }

    @staticmethod
    def compute_pivot_support_resistance(closes: List[float], current: float) -> Dict:
        recent = closes[-20:] if len(closes) >= 20 else closes
        h, l = max(recent), min(recent)
        pivot = (h + l + current) / 3
        return {
            "support":    [round(2*pivot - h, 2), round(pivot - (h - l), 2)],
            "resistance": [round(2*pivot - l, 2), round(pivot + (h - l), 2)],
            "pivot":      round(pivot, 2),
        }

    @staticmethod
    def trend_detection(prices: List[float]) -> str:
        if len(prices) < 20:
            return "sideways"
        ma5  = np.mean(prices[-5:])
        ma20 = np.mean(prices[-20:])
        if ma5 > ma20 * 1.005:
            return "bullish"
        elif ma5 < ma20 * 0.995:
            return "bearish"
        return "sideways"

    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> float:
        return TechnicalIndicators._rsi(prices, period)

    @staticmethod
    def macd(prices: List[float]) -> Dict:
        return TechnicalIndicators._macd(prices)
