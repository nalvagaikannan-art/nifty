"""
Strategy Signal History — In-Memory Store
==========================================
ஒவ்வொரு refresh-லும் best strategy signal-ஐ save செய்கிறோம்.
Last 20 signals per symbol track செய்கிறோம்.
Phase 3: Strategy History + Signal Reversal tracking.
"""
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional
import threading

# Thread-safe signal history
_lock    = threading.Lock()
_history: Dict[str, deque] = {}   # {symbol: deque(maxlen=20)}
MAX_HISTORY = 20


def record_signal(
    symbol:    str,
    strategy:  str,
    score:     int,
    market_state: str,
    confidence: int,
    spot:      float,
    pcr:       float,
    vix:       float,
    reasons:   List[str] = None,
) -> dict:
    """
    New signal record செய்கிறோம்.
    Signal மாறியிருந்தால் 'reversal' flag செய்கிறோம்.
    Returns: signal dict with reversal info.
    """
    with _lock:
        if symbol not in _history:
            _history[symbol] = deque(maxlen=MAX_HISTORY)

        hist = _history[symbol]
        prev = hist[-1] if hist else None
        prev_strategy = prev["strategy"] if prev else None

        # Signal reversal detection
        reversal = False
        reversal_type = ""
        if prev_strategy and prev_strategy != strategy:
            if strategy == "WAIT":
                reversal_type = f"{prev_strategy} → WAIT"
            elif prev_strategy == "WAIT":
                reversal_type = f"WAIT → {strategy}"
            else:
                # Direct reversal: CALL→PUT or PUT→CALL
                reversal_type = f"{prev_strategy} → {strategy}"
                reversal = True

        signal = {
            "symbol":       symbol,
            "strategy":     strategy,
            "score":        score,
            "market_state": market_state,
            "confidence":   confidence,
            "spot":         spot,
            "pcr":          round(pcr, 3) if pcr else 0,
            "vix":          round(vix, 1) if vix else 0,
            "reversal":     reversal,
            "reversal_type": reversal_type,
            "timestamp":    datetime.now().strftime("%H:%M:%S"),
            "date":         datetime.now().strftime("%d-%b"),
        }
        hist.append(signal)
        return signal


def get_history(symbol: str) -> List[dict]:
    """Last N signals for symbol — newest first."""
    with _lock:
        hist = _history.get(symbol, deque())
        return list(reversed(list(hist)))


def get_reversals(symbol: str) -> List[dict]:
    """Only reversal signals."""
    return [s for s in get_history(symbol) if s.get("reversal")]


def clear_history(symbol: str = None) -> None:
    with _lock:
        if symbol:
            _history.pop(symbol, None)
        else:
            _history.clear()
