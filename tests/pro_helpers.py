"""Synthetic option chain for NIFTY-PRO tests."""
import math


def make_chain(spot=22500.0, iv=14.0, step=50, lo=21500, hi=23500, expiry="17NOV2026", lot_size=65):
    rows = []
    k = lo
    while k <= hi:
        tv = 120 * math.exp(-(((k - spot) / 250) ** 2)) + 2
        ce = max(spot - k, 0) + tv
        pe = max(k - spot, 0) + tv
        ce_oi = int(1e5 * math.exp(-(((k - 22700) / 200) ** 2)) + 1e4)
        pe_oi = int(1e5 * math.exp(-(((k - 22300) / 200) ** 2)) + 1e4)

        def q(p, oi):
            return {"openInterest": oi, "changeinOpenInterest": 0, "totalTradedVolume": 1000, "lastPrice": round(p, 2),
                    "impliedVolatility": iv, "bidprice": round(p * 0.995, 2), "askPrice": round(p * 1.005, 2)}
        rows.append({"strikePrice": k, "CE": q(ce, ce_oi), "PE": q(pe, pe_oi)})
        k += step
    return {"symbol": "NIFTY", "expiry": expiry, "underlying_price": spot, "lot_size": lot_size, "data": rows,
            "data_source": "test"}


def build_ctx(spot=22500.0, iv_rank=80.0, n=60, adx=15.0, dte=3, vix=15.0, **kw):
    from app.services.option_analyzer import OptionAnalyzer
    from app.services.pro_metrics import chain_metrics
    chain = make_chain(spot=spot, **kw)
    df = OptionAnalyzer().process_option_chain(chain)
    m = chain_metrics(df, spot)
    iv = {"rank": iv_rank, "percentile": iv_rank, "n": n, "reliable": n >= 30, "min_samples": 30}
    return {"symbol": "NIFTY", "spot": spot, "df": df, "expiry": chain["expiry"], "dte": dte, "vix": vix,
            "iv": iv, "metrics": m, "adx": adx, "chain": chain}
