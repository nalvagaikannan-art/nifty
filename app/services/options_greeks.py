"""
Options Greeks (Black-Scholes, European approximation for NSE index options)
================================================================================
Review point #15: "NIFTY spot direction correct இருந்தாலும் option buyer loss
ஆகலாம் — இதுதான் தற்போதைய Accuracy engine-ல் தெரியவில்லை." Theta decay and IV
crush can lose money on a directionally-correct trade. This module computes
the standard Greeks (delta, gamma, theta, vega) from data this app already
has (spot, strike, days-to-expiry, IV from the option chain) so strategy
output and the accuracy engine can account for them instead of pretending
spot-direction is the whole story.

NSE index options are European-exercise, so Black-Scholes (no early-exercise
adjustment) is the correct — not just convenient — model here, unlike US
single-stock options where it would be an approximation.

Deliberately dependency-free (no scipy) — norm.cdf/pdf implemented with
math.erf, which is exact (not a numeric approximation of the approximation).
"""
import math
from typing import Dict, Optional

RISK_FREE_RATE = 0.065  # India ~91-day T-bill proxy; stable enough not to need live data
TRADING_DAYS_PER_YEAR = 365.0  # calendar days for theta decay (weekends still decay time value)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _black_scholes_price(
    spot: float,
    strike: float,
    days_to_expiry: float,
    iv_pct: float,
    option_type: str,
    r: float = RISK_FREE_RATE,
) -> Optional[float]:
    """European Black-Scholes option price for IV inversion."""
    if (
        spot <= 0
        or strike <= 0
        or days_to_expiry is None
        or days_to_expiry <= 0
        or iv_pct <= 0
    ):
        return None

    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        return None

    sigma = iv_pct / 100.0
    t = days_to_expiry / TRADING_DAYS_PER_YEAR

    if sigma <= 0 or t <= 0:
        return None

    sqrt_t = math.sqrt(t)

    try:
        d1 = (
            math.log(spot / strike)
            + (r + 0.5 * sigma * sigma) * t
        ) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
    except (ValueError, ZeroDivisionError):
        return None

    discount = math.exp(-r * t)

    if option_type == "CE":
        price = (
            spot * _norm_cdf(d1)
            - strike * discount * _norm_cdf(d2)
        )
    else:
        price = (
            strike * discount * _norm_cdf(-d2)
            - spot * _norm_cdf(-d1)
        )

    return price if math.isfinite(price) else None


def implied_volatility_from_price(
    spot: float,
    strike: float,
    days_to_expiry: float,
    option_price: float,
    option_type: str,
    r: float = RISK_FREE_RATE,
    min_iv_pct: float = 0.1,
    max_iv_pct: float = 500.0,
    tolerance: float = 0.01,
    max_iterations: int = 80,
) -> Optional[float]:
    """
    Derive IV (%) from an observed option premium using dependency-free
    Black-Scholes bisection.

    This is deliberately a fallback for feeds that do not provide IV.
    It never fabricates an IV for expiry-day/expired contracts.
    """
    if (
        spot <= 0
        or strike <= 0
        or days_to_expiry is None
        or days_to_expiry <= 0
        or option_price <= 0
    ):
        return None

    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        return None

    # Basic no-arbitrage lower bound.
    t = days_to_expiry / TRADING_DAYS_PER_YEAR
    discount = math.exp(-r * t)

    if option_type == "CE":
        intrinsic = max(0.0, spot - strike * discount)
    else:
        intrinsic = max(0.0, strike * discount - spot)

    # Allow a tiny numerical tolerance around intrinsic value.
    if option_price < intrinsic - 1e-6:
        return None

    low = float(min_iv_pct)
    high = float(max_iv_pct)

    low_price = _black_scholes_price(
        spot, strike, days_to_expiry, low, option_type, r
    )
    high_price = _black_scholes_price(
        spot, strike, days_to_expiry, high, option_type, r
    )

    if low_price is None or high_price is None:
        return None

    # Premium must be bracketed by the IV search range.
    if option_price < low_price - tolerance:
        return None

    if option_price > high_price + tolerance:
        return None

    for _ in range(max_iterations):
        mid = (low + high) / 2.0
        mid_price_value = _black_scholes_price(
            spot, strike, days_to_expiry, mid, option_type, r
        )

        if mid_price_value is None:
            return None

        diff = mid_price_value - option_price

        if abs(diff) <= tolerance:
            return round(mid, 4)

        if diff > 0:
            high = mid
        else:
            low = mid

    result = (low + high) / 2.0
    return round(result, 4) if math.isfinite(result) else None


def black_scholes_greeks(
    spot: float,
    strike: float,
    days_to_expiry: float,
    iv_pct: float,
    option_type: str,
    r: float = RISK_FREE_RATE,
) -> Optional[Dict]:
    """
    iv_pct: implied volatility as a PERCENTAGE (e.g. 14.5), matching how
    this app already stores IV everywhere else (option chain's
    `impliedVolatility` field, `ce_iv`/`pe_iv` columns) — converted to
    decimal internally.

    Returns None (never a fabricated number) when inputs can't support a
    real calculation: spot/strike/iv <= 0, or days_to_expiry <= 0 (already
    expired / expiring today has degenerate theta — expiry-day risk is
    already surfaced separately via utils.helpers.expiry_filter).
    """
    if spot <= 0 or strike <= 0 or iv_pct <= 0 or days_to_expiry is None or days_to_expiry <= 0:
        return None

    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        return None

    sigma = iv_pct / 100.0
    t = days_to_expiry / TRADING_DAYS_PER_YEAR
    sqrt_t = math.sqrt(t)

    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
    except (ValueError, ZeroDivisionError):
        return None

    pdf_d1 = _norm_pdf(d1)
    is_call = option_type == "CE"

    if is_call:
        delta = _norm_cdf(d1)
        theta_annual = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            - r * strike * math.exp(-r * t) * _norm_cdf(d2)
        )
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            + r * strike * math.exp(-r * t) * _norm_cdf(-d2)
        )

    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0          # per 1 IV point (not 1.00 = 100%)
    theta_per_day = theta_annual / TRADING_DAYS_PER_YEAR  # per calendar day

    return {
        "delta":       round(delta, 4),
        "gamma":       round(gamma, 6),
        "theta_per_day": round(theta_per_day, 2),  # ₹ premium lost per day, all else equal
        "vega":        round(vega, 2),              # ₹ premium change per 1 IV-point move
        "days_to_expiry": days_to_expiry,
        "iv_used_pct": iv_pct,
    }


def mid_price(bid: float, ask: float, ltp: float) -> float:
    """Executable-price estimate (review #13): LTP is the last TRADE, which
    can be stale or off a thin book — the bid/ask midpoint is what a market
    order would actually fill near, right now. Falls back to LTP only when
    bid/ask aren't usable (0, missing, or crossed/inverted quotes)."""
    if bid and ask and ask > bid > 0:
        return round((bid + ask) / 2.0, 2)
    return round(ltp, 2) if ltp else 0.0


def spread_pct(bid: float, ask: float) -> Optional[float]:
    """Bid/ask spread as % of mid — a direct liquidity/slippage signal
    (review #13/#14) that OI and volume alone don't capture: two strikes can
    have identical OI but very different spreads."""
    if not (bid and ask and ask > bid > 0):
        return None
    mid = (bid + ask) / 2.0
    return round((ask - bid) / mid * 100, 2) if mid > 0 else None
