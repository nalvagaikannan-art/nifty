"""
OptionAnalyzer — PCR, Max Pain, OI Summary, CE/PE strike analysis
"""
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from app.utils.helpers import safe_float, safe_int, days_to_expiry
from app.services.options_greeks import black_scholes_greeks, mid_price, spread_pct


class OptionAnalyzer:

    def process_option_chain(self, chain_data: Dict) -> pd.DataFrame:
        records = []
        for item in chain_data.get("data", []):
            strike = safe_float(item.get("strikePrice"))
            ce = item.get("CE", {}) or {}
            pe = item.get("PE", {}) or {}
            ce_bid, ce_ask = safe_float(ce.get("bidprice", 0)), safe_float(ce.get("askPrice", 0))
            pe_bid, pe_ask = safe_float(pe.get("bidprice", 0)), safe_float(pe.get("askPrice", 0))
            ce_ltp = safe_float(ce.get("lastPrice", 0))
            pe_ltp = safe_float(pe.get("lastPrice", 0))
            records.append({
                "strike":       strike,
                "ce_oi":        safe_int(ce.get("openInterest", 0)),
                "ce_oi_chg":    safe_int(ce.get("changeinOpenInterest", 0)),
                "ce_volume":    safe_int(ce.get("totalTradedVolume", 0)),
                "ce_ltp":       ce_ltp,
                "ce_iv":        safe_float(ce.get("impliedVolatility", 0)),
                "ce_bid":       ce_bid,
                "ce_ask":       ce_ask,
                # Executable-price estimate (review #13) — mid-of-book when
                # a usable quote exists, else LTP. Used for entry/target/SL
                # calcs instead of raw LTP so figures reflect what a market
                # order would actually fill near.
                "ce_mid":       mid_price(ce_bid, ce_ask, ce_ltp),
                "ce_spread_pct": spread_pct(ce_bid, ce_ask),
                "pe_oi":        safe_int(pe.get("openInterest", 0)),
                "pe_oi_chg":    safe_int(pe.get("changeinOpenInterest", 0)),
                "pe_volume":    safe_int(pe.get("totalTradedVolume", 0)),
                "pe_ltp":       pe_ltp,
                "pe_iv":        safe_float(pe.get("impliedVolatility", 0)),
                "pe_bid":       pe_bid,
                "pe_ask":       pe_ask,
                "pe_mid":       mid_price(pe_bid, pe_ask, pe_ltp),
                "pe_spread_pct": spread_pct(pe_bid, pe_ask),
            })
        df = pd.DataFrame(records)
        return df if not df.empty else pd.DataFrame()

    def attach_greeks(self, df: pd.DataFrame, spot: float, expiry_str: str) -> pd.DataFrame:
        """Adds ce_delta/ce_theta_per_day/ce_vega/ce_gamma (and pe_*) columns.
        Review #15: Greeks were entirely missing — this is what lets a
        strategy/accuracy view show "this CE loses ~₹X/day to theta even if
        NIFTY doesn't move", not just direction. Silently no-ops (returns df
        unchanged) if expiry can't be parsed or spot is invalid — never
        fabricates a days-to-expiry number."""
        if df.empty or spot <= 0:
            return df
        dte = days_to_expiry(expiry_str)
        if dte is None:
            return df
        # Expiry day itself (dte=0) has a degenerate/undefined theta in
        # Black-Scholes (division by sqrt(t)→0) — floor at a small fraction
        # of a day so the column still populates instead of going NaN.
        dte_for_calc = max(dte, 1) if dte <= 0 else dte

        def _row_greeks(row, side):
            iv = row.get(f"{side}_iv", 0)
            g = black_scholes_greeks(spot, row["strike"], dte_for_calc, iv,
                                      "CE" if side == "ce" else "PE")
            return g or {}

        for side in ("ce", "pe"):
            greeks = df.apply(lambda r: _row_greeks(r, side), axis=1)
            df[f"{side}_delta"]         = greeks.apply(lambda g: g.get("delta"))
            df[f"{side}_gamma"]         = greeks.apply(lambda g: g.get("gamma"))
            df[f"{side}_theta_per_day"] = greeks.apply(lambda g: g.get("theta_per_day"))
            df[f"{side}_vega"]          = greeks.apply(lambda g: g.get("vega"))
        return df

    def compute_pcr(self, df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        total_pe = df["pe_oi"].sum()
        total_ce = df["ce_oi"].sum()
        return round(total_pe / total_ce, 3) if total_ce > 0 else 0.0

    def compute_max_pain(self, df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        strikes = df["strike"].to_numpy()
        ce_oi   = df["ce_oi"].to_numpy()
        pe_oi   = df["pe_oi"].to_numpy()
        cands   = strikes.reshape(-1, 1)
        actual  = strikes.reshape(1, -1)
        ce_loss = ce_oi.reshape(1, -1) * np.maximum(0, cands - actual)
        pe_loss = pe_oi.reshape(1, -1) * np.maximum(0, actual - cands)
        total   = (ce_loss + pe_loss).sum(axis=1)
        return float(strikes[int(np.argmin(total))])

    def compute_oi_change(self, df: pd.DataFrame) -> Dict:
        if df.empty:
            return {"ce_change": 0, "pe_change": 0, "total_change": 0}
        ce = int(df["ce_oi_chg"].sum())
        pe = int(df["pe_oi_chg"].sum())
        return {"ce_change": ce, "pe_change": pe, "total_change": ce + pe}

    def oi_summary(self, df: pd.DataFrame, underlying: float = 0) -> Dict:
        """CE/PE max OI strike, total OI, ATM IV, and live chain volume.

        `underlying` is optional (default 0, backward compatible with
        existing callers that don't pass it) — when given, also computes
        the true ATM strike's CE/PE OI and CE-PE IV skew, used by the
        terminal dashboard's option-chain panel.
        """
        if df.empty:
            return {}
        ce_max_idx = df["ce_oi"].idxmax()
        pe_max_idx = df["pe_oi"].idxmax()
        # ATM IV = average of CE and PE IV at max OI strikes
        atm_iv = (df.loc[ce_max_idx, "ce_iv"] + df.loc[pe_max_idx, "pe_iv"]) / 2
        result = {
            "ce_max_oi_strike": float(df.loc[ce_max_idx, "strike"]),
            "pe_max_oi_strike": float(df.loc[pe_max_idx, "strike"]),
            "total_ce_oi":      int(df["ce_oi"].sum()),
            "total_pe_oi":      int(df["pe_oi"].sum()),
            "atm_iv":           round(float(atm_iv), 2),
            **self.compute_option_volume(df),
        }
        if underlying > 0:
            atm_idx = (df["strike"] - underlying).abs().idxmin()
            result["atm_strike"]  = float(df.loc[atm_idx, "strike"])
            result["atm_call_oi"] = int(df.loc[atm_idx, "ce_oi"])
            result["atm_put_oi"]  = int(df.loc[atm_idx, "pe_oi"])
            ce_iv_atm = float(df.loc[atm_idx, "ce_iv"])
            pe_iv_atm = float(df.loc[atm_idx, "pe_iv"])
            if ce_iv_atm > 0 and pe_iv_atm > 0:
                result["iv_skew"] = round(pe_iv_atm - ce_iv_atm, 2)
        return result

    def compute_option_volume(self, df: pd.DataFrame) -> Dict:
        """
        Today's live CE/PE traded volume from the option chain snapshot —
        a single-point-in-time reading (not a historical series), unlike
        the underlying's own volume which is usually unavailable for
        indices. Also used as a last-resort volume signal when neither
        Angel One's index candles nor its futures candles have volume data
        (see data_fetcher._try_angel_historical).

        volume_pcr follows the same reading convention as OI-PCR: high
        PE/CE volume ratio = more put activity = typically bullish
        (put writing/buying context dependent), low = bearish.
        """
        if df.empty:
            return {"total_ce_volume": 0, "total_pe_volume": 0, "volume_pcr": 0.0}
        total_ce_vol = int(df["ce_volume"].sum())
        total_pe_vol = int(df["pe_volume"].sum())
        volume_pcr = round(total_pe_vol / total_ce_vol, 3) if total_ce_vol > 0 else 0.0
        return {
            "total_ce_volume": total_ce_vol,
            "total_pe_volume": total_pe_vol,
            "volume_pcr":      volume_pcr,
        }

    def pick_candidates(self, df: pd.DataFrame, underlying: float) -> Dict:
        """Best CE/PE candidate near ATM — for the beginner-friendly option
        view. Deterministic, no ML: within ATM ±3 strikes, picks the strike
        with the strongest *rising* OI (ce_oi_chg / pe_oi_chg > 0) — i.e.
        where fresh positions are actively building today, not just where
        OI happens to be largest historically.

        Score (0-100) is a transparent weighted blend of only real,
        already-fetched numbers, documented in `score_basis` so it's never
        a black box:
          - OI size vs the ATM±3 window   (40%)
          - Volume vs the ATM±3 window    (30%)
          - Today's OI buildup vs the window's strongest buildup (30%)
        """
        if df.empty or underlying <= 0:
            return {"ce": None, "pe": None}

        window = self.chain_for_display(df, underlying, n_strikes=3)
        if not window:
            return {"ce": None, "pe": None}
        wdf = pd.DataFrame(window)

        def _best(side: str) -> Optional[Dict]:
            oi_col, chg_col, vol_col, ltp_col = f"{side}_oi", f"{side}_oi_chg", f"{side}_volume", f"{side}_ltp"
            rising = wdf[wdf[chg_col] > 0]
            pool = rising if not rising.empty else wdf  # no fresh buildup anywhere → fall back to largest OI
            if pool.empty or pool[oi_col].max() <= 0:
                return None
            idx = pool[chg_col].idxmax() if not rising.empty else pool[oi_col].idxmax()
            row = pool.loc[idx]

            max_oi  = max(wdf[oi_col].max(), 1)
            max_vol = max(wdf[vol_col].max(), 1)
            max_chg = max(wdf[chg_col].max(), 1)
            oi_score  = (row[oi_col]  / max_oi)  * 40
            vol_score = (row[vol_col] / max_vol) * 30
            chg_score = (max(row[chg_col], 0) / max_chg) * 30
            score = round(oi_score + vol_score + chg_score, 1)

            distance_strikes = round(abs(row["strike"] - underlying) / 50)
            risk = "Low" if distance_strikes == 0 else ("Medium" if distance_strikes == 1 else "High")

            return {
                "strike":        float(row["strike"]),
                "ltp":           safe_float(row[ltp_col]),
                "oi":            int(row[oi_col]),
                "oi_change":     int(row[chg_col]),
                "volume":        int(row[vol_col]),
                "is_atm":        bool(row.get("is_atm", False)),
                "distance_strikes": int(distance_strikes),
                "risk":          risk,
                "score":         min(100.0, score),
                "score_basis":   "40% OI size + 30% volume + 30% today's OI buildup, within ATM±3 strikes",
            }

        return {"ce": _best("ce"), "pe": _best("pe")}

    def chain_for_display(self, df: pd.DataFrame, underlying: float, n_strikes: int = 10) -> List[Dict]:
        """Return ±n strikes around ATM for frontend table."""
        if df.empty or underlying <= 0:
            return []
        atm_idx  = (df["strike"] - underlying).abs().idxmin()
        atm_strike = df.loc[atm_idx, "strike"]
        filtered = df[
            (df["strike"] >= atm_strike - n_strikes * 50) &
            (df["strike"] <= atm_strike + n_strikes * 50)
        ].copy()
        filtered["is_atm"] = filtered["strike"] == atm_strike
        return filtered.to_dict("records")
