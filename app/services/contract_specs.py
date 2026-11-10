"""
Contract sizing metadata.

Angel instrument-master lotsize is authoritative when the resolved option
chain provides it. These values are current-contract fallbacks for provider
paths that do not expose contract metadata.
"""

CURRENT_CONTRACT_LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
}


def resolve_lot_size(symbol: str, chain=None) -> int:
    """Return exact chain lot_size when available, else current fallback."""
    if isinstance(chain, dict):
        try:
            lot_size = int(float(chain.get("lot_size") or 0))
        except (TypeError, ValueError):
            lot_size = 0
        if lot_size > 0:
            return lot_size

    return CURRENT_CONTRACT_LOT_SIZES.get(str(symbol or "").upper(), 0)
