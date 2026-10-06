import asyncio
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, Optional, List

from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from app.config import settings
from app.utils.helpers import is_market_hours_ist
from app.services.angel_one import angel_session
from app.utils.helpers import epoch_ms_to_ist

logger = logging.getLogger(__name__)


class _CompatibleSmartWebSocketV2(SmartWebSocketV2):
    """Compatibility shim for websocket-client close callback signatures.

    websocket-client 1.9.x may invoke the close callback with:
        (wsapp, close_status_code, close_msg)

    Older SmartApi SmartWebSocketV2 expects:
        (wsapp)

    Keep the original SmartApi callback behavior while accepting
    the newer websocket-client callback signature.
    """

    def _on_close(self, wsapp, close_status_code=None, close_msg=None):
        self.on_close(wsapp)


class AngelLiveFeed:
    """
    Angel One WebSocket V2 live LTP feed.

    Current scope:
      - NIFTY
      - BANKNIFTY
      - FINNIFTY
      - LTP mode
      - in-memory tick snapshots

    Candle aggregation will be added only after the
    WebSocket lifecycle is verified independently.
    """

    SYMBOL_TOKENS = {
        "NIFTY": "99926000",
        "BANKNIFTY": "99926009",
        "FINNIFTY": "99926037",
        "SENSEX": "99919000",
    }

    EXCHANGE_TYPE = 1
    BSE_EXCHANGE_TYPE = 3
    MODE_LTP = 1

    # OPTION_WS_PATCH_20260923
    # Angel NFO option stream.
    OPTION_EXCHANGE_TYPE = 2
    BFO_OPTION_EXCHANGE_TYPE = 4
    OPTION_MODE_SNAP_QUOTE = 3
    OPTION_REFRESH_INTERVAL_SEC = 60

    # A market-data watchdog is deliberately separate from the 10s
    # per-tick freshness gate in data_fetcher.py.  The watchdog detects a
    # connected-but-silent/stuck socket and only operates during NSE hours.
    WATCHDOG_INTERVAL_SEC = 10
    LIVE_TICK_MAX_AGE_SEC = 10
    STALE_TICK_RECONNECT_SEC = 60

    def __init__(self):
        self._ws: Optional[SmartWebSocketV2] = None
        self._thread: Optional[threading.Thread] = None

        self._stop_event = threading.Event()
        self._connected = threading.Event()

        self._lock = threading.Lock()

        self._ticks: Dict[str, dict] = {}
        self._token_to_symbol = {
            token: symbol
            for symbol, token in self.SYMBOL_TOKENS.items()
        }

        self._started_at: Optional[float] = None
        self._last_error: Optional[str] = None

        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_tick_received_at: Dict[str, float] = {}
        self._last_exchange_timestamp: Dict[str, float] = {}

        # LOCAL_5M_CANDLE_BUILDER_20260923
        # Index WebSocket provides LTP, not traded index volume.
        # Therefore local candle volume is deliberately 0.
        # Missing 5-minute buckets are never synthesized.
        self._local_5m_current: Dict[str, dict] = {}
        self._local_5m_completed: Dict[str, List[dict]] = defaultdict(list)
        self._local_5m_max_candles = 300

        # OPTION_WS_PATCH_20260923
        self._option_ticks: Dict[str, dict] = {}
        self._option_token_meta: Dict[str, dict] = {}
        self._option_tokens_by_symbol: Dict[str, set] = {}
        self._option_refresh_thread: Optional[threading.Thread] = None
        self._option_refresh_stop = threading.Event()
        self._option_refresh_now = threading.Event()
        self._option_last_refresh_at: Optional[float] = None
        self._option_last_error: Optional[str] = None
        # OPTION_REFRESH_EVENTLOOP_FIX_20260923
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> bool:
        """Ensure Angel session exists, then start WS in a background thread."""

        if self._thread and self._thread.is_alive():
            return True

        await angel_session.ensure_session()
        # OPTION_REFRESH_EVENTLOOP_FIX_20260923
        self._async_loop = asyncio.get_running_loop()

        auth_token = getattr(angel_session, "_auth_token", None)
        feed_token = getattr(angel_session, "_feed_token", None)
        api_key = getattr(settings, "angel_api_key", None)
        client_code = getattr(settings, "angel_client_id", None)

        if not all((auth_token, feed_token, api_key, client_code)):
            self._last_error = "Angel WebSocket credentials/session not available"
            logger.warning(self._last_error)
            return False

        self._stop_event.clear()
        self._connected.clear()
        self._last_error = None
        self._started_at = time.time()

        # OPTION_WS_PATCH_20260923
        self._option_refresh_stop.clear()
        self._option_refresh_now.clear()
        self._start_option_refresh_worker()

        self._thread = threading.Thread(
            target=self._run,
            name="angel-live-feed",
            daemon=True,
        )
        self._thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="angel-live-feed-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

        logger.info("Angel live feed thread started")
        return True

    def stop(self):
        """Stop the WebSocket and background thread."""

        self._stop_event.set()
        self._connected.clear()

        # OPTION_WS_PATCH_20260923
        self._option_refresh_stop.set()
        self._option_refresh_now.set()

        ws = self._ws
        if ws is not None:
            try:
                ws.close_connection()
            except Exception:
                logger.exception("Error closing Angel WebSocket")

        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

        watchdog = self._watchdog_thread
        if watchdog and watchdog.is_alive():
            watchdog.join(timeout=2)

        self._thread = None
        self._watchdog_thread = None
        self._ws = None

        logger.info("Angel live feed stopped")

    def _run(self):
        while not self._stop_event.is_set():
            # Do not open/reopen an Angel market-data socket outside the
            # regular NSE session. This avoids after-hours reconnect loops.
            if not is_market_hours_ist():
                self._connected.clear()
                self._stop_event.wait(30)
                continue

            try:
                auth_token = getattr(angel_session, "_auth_token", None)
                feed_token = getattr(angel_session, "_feed_token", None)
                api_key = getattr(settings, "angel_api_key", None)
                client_code = getattr(settings, "angel_client_id", None)

                self._ws = _CompatibleSmartWebSocketV2(
                    auth_token,
                    api_key,
                    client_code,
                    feed_token,
                    max_retry_attempt=3,
                    retry_strategy=1,
                    retry_delay=2,
                    retry_multiplier=2,
                    retry_duration=30,
                )

                self._ws.on_open = self._on_open
                self._ws.on_data = self._on_data
                self._ws.on_error = self._on_error
                self._ws.on_close = self._on_close

                logger.info("Connecting to Angel WebSocket V2")
                self._ws.connect()

            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("Angel live feed crashed")

            finally:
                self._connected.clear()

            if self._stop_event.is_set():
                break

            # The SDK handles its own on_error retries. If connect() exits
            # completely (for example after watchdog-triggered close), give
            # it a small controlled pause before a fresh socket cycle.
            self._stop_event.wait(5)

    def _watchdog_loop(self):
        while not self._stop_event.wait(self.WATCHDOG_INTERVAL_SEC):
            market_open = is_market_hours_ist()

            # ANGEL_WS_SESSION_CLOSE_FIX_20260922
            # A socket that was connected during market hours can remain
            # open after 15:30 IST. Close it explicitly instead of allowing
            # after-hours callbacks/heartbeats to look like live market data.
            if not market_open:
                if self._connected.is_set():
                    self._connected.clear()

                    ws = self._ws
                    if ws is not None:
                        logger.info(
                            "Angel WebSocket market session closed; "
                            "closing live feed"
                        )
                        try:
                            ws.close_connection()
                        except Exception:
                            logger.exception(
                                "Angel WebSocket session-close shutdown failed"
                            )
                continue

            if not self._connected.is_set():
                continue

            now = time.time()
            stale_symbols = []

            with self._lock:
                for symbol in self.SYMBOL_TOKENS:
                    received_at = self._last_tick_received_at.get(symbol, 0.0)
                    exchange_ts = self._last_exchange_timestamp.get(symbol, 0.0)

                    received_age = (
                        now - received_at if received_at > 0 else float("inf")
                    )
                    exchange_age = (
                        now - exchange_ts if exchange_ts > 0 else float("inf")
                    )

                    if (
                        received_age > self.STALE_TICK_RECONNECT_SEC
                        or exchange_age > self.STALE_TICK_RECONNECT_SEC
                    ):
                        stale_symbols.append(symbol)

            if not stale_symbols:
                continue

            message = (
                  "Angel WebSocket watchdog: stale/no tick for subscribed "
                  "symbol(s) for >%ss; reconnecting"
                % self.STALE_TICK_RECONNECT_SEC
            )
            self._last_error = message
            logger.warning("%s symbols=%s", message, ",".join(stale_symbols))

            self._connected.clear()

            ws = self._ws
            if ws is not None:
                try:
                    ws.close_connection()
                except Exception:
                    logger.exception(
                        "Angel WebSocket watchdog close failed"
                    )

    def _on_open(self, wsapp):
        logger.info("Angel WebSocket connected")

        token_list = []

        nse_tokens = [
            token
            for symbol, token in self.SYMBOL_TOKENS.items()
            if symbol != "SENSEX"
        ]
        if nse_tokens:
            token_list.append({
                "exchangeType": self.EXCHANGE_TYPE,
                "tokens": nse_tokens,
            })

        if "SENSEX" in self.SYMBOL_TOKENS:
            token_list.append({
                "exchangeType": self.BSE_EXCHANGE_TYPE,
                "tokens": [self.SYMBOL_TOKENS["SENSEX"]],
            })

        try:
            self._ws.subscribe(
                "nifty-live-feed",
                self.MODE_LTP,
                token_list,
            )

            with self._lock:
                self._last_tick_received_at.clear()
                self._last_exchange_timestamp.clear()

                # OPTION_WS_PATCH_20260923
                # Token mappings belong to this socket connection.
                self._option_ticks.clear()
                self._option_token_meta.clear()
                self._option_tokens_by_symbol.clear()

            self._connected.set()

            logger.info(
                "Angel WebSocket subscribed: "
                "NIFTY/BANKNIFTY/FINNIFTY(NSE) + SENSEX(BSE)"
            )

            # Rebuild NFO subscriptions immediately after every
            # successful socket connection.
            self._option_refresh_now.set()

        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("Angel WebSocket subscribe failed")

    # ============================================================
    # OPTION_WS_PATCH_20260923
    # ============================================================

    def _start_option_refresh_worker(self):
        if (
            self._option_refresh_thread
            and self._option_refresh_thread.is_alive()
        ):
            return

        self._option_refresh_thread = threading.Thread(
            target=self._option_refresh_loop,
            name="angel-option-refresh",
            daemon=True,
        )
        self._option_refresh_thread.start()

    def _option_refresh_loop(self):
        while not self._option_refresh_stop.is_set():
            if not self._connected.is_set():
                self._option_refresh_now.wait(2)
                self._option_refresh_now.clear()
                continue

            try:
                # OPTION_REFRESH_EVENTLOOP_FIX_20260923
                loop = self._async_loop
                if loop is None or loop.is_closed():
                    raise RuntimeError(
                        "Angel option refresh event loop is unavailable"
                    )

                future = asyncio.run_coroutine_threadsafe(
                    self._refresh_option_subscriptions(),
                    loop,
                )
                future.result()
            except Exception as exc:
                self._option_last_error = str(exc)
                logger.exception(
                    "Angel option websocket refresh failed"
                )

            self._option_refresh_now.wait(
                self.OPTION_REFRESH_INTERVAL_SEC
            )
            self._option_refresh_now.clear()

    async def _refresh_option_subscriptions(self):
        for symbol in self.SYMBOL_TOKENS:
            try:
                chain = await angel_session.get_option_chain(
                    symbol
                )

                desired_meta = {}

                for row in (chain.get("data") if isinstance(chain, dict) else chain) or []:
                    for leg_name in ("CE", "PE"):
                        leg = row.get(leg_name)

                        if not isinstance(leg, dict):
                            continue

                        token = leg.get("token")
                        if token is None:
                            continue

                        token = str(token).strip()
                        if not token:
                            continue

                        desired_meta[token] = {
                            "symbol": symbol,
                            "token": token,
                            "expiry": leg.get("expiryDate"),
                            "strike": leg.get("strikePrice"),
                            "optionType": str(
                                leg.get("optionType")
                                or leg_name
                            ).upper(),
                            "tradingsymbol": leg.get(
                                "tradingsymbol"
                            ),
                        }

                if not desired_meta:
                    logger.warning(
                        "Angel option chain returned no tokens: %s",
                        symbol,
                    )
                    continue

                desired_tokens = set(desired_meta)
                option_exchange_type = (
                    self.BFO_OPTION_EXCHANGE_TYPE
                    if symbol == "SENSEX"
                    else self.OPTION_EXCHANGE_TYPE
                )


                with self._lock:
                    current_tokens = set(
                        self._option_tokens_by_symbol.get(
                            symbol,
                            set(),
                        )
                    )

                stale_tokens = (
                    current_tokens - desired_tokens
                )
                new_tokens = (
                    desired_tokens - current_tokens
                )

                # ------------------------------------------------
                # Remove obsolete tokens
                # ------------------------------------------------
                if stale_tokens:
                    try:
                        self._ws.unsubscribe(
                            "nifty-live-options",
                            self.OPTION_MODE_SNAP_QUOTE,
                            [{
                                "exchangeType":
                                    option_exchange_type,
                                "tokens":
                                    sorted(stale_tokens),
                            }],
                        )

                        with self._lock:
                            for token in stale_tokens:
                                self._option_token_meta.pop(
                                    token,
                                    None,
                                )
                                self._option_ticks.pop(
                                    token,
                                    None,
                                )

                        logger.info(
                            "Angel option websocket unsubscribed: "
                            "%s tokens=%s",
                            symbol,
                            len(stale_tokens),
                        )

                    except Exception:
                        logger.exception(
                            "Angel option websocket unsubscribe "
                            "failed: %s",
                            symbol,
                        )

                # ------------------------------------------------
                # Add new tokens
                # ------------------------------------------------
                if new_tokens:
                    try:
                        self._ws.subscribe(
                            "nifty-live-options",
                            self.OPTION_MODE_SNAP_QUOTE,
                            [{
                                "exchangeType":
                                    option_exchange_type,
                                "tokens":
                                    sorted(new_tokens),
                            }],
                        )

                        # Only record successful subscriptions.
                        # Failed tokens remain absent and will retry
                        # on the next refresh.
                        with self._lock:
                            for token in new_tokens:
                                self._option_token_meta[token] = (
                                    desired_meta[token]
                                )

                            self._option_tokens_by_symbol[symbol] = (
                                set(desired_tokens)
                            )

                        logger.info(
                            "Angel option websocket subscribed: "
                            "%s tokens=%s mode=SNAP_QUOTE exchange=%s",
                            symbol,
                            len(new_tokens),
                              "BFO" if symbol == "SENSEX" else "NFO",
                        )

                    except Exception:
                        logger.exception(
                            "Angel option websocket subscribe "
                            "failed: %s tokens=%s",
                            symbol,
                            len(new_tokens),
                        )

                elif desired_tokens == current_tokens:
                    with self._lock:
                        for token, meta in desired_meta.items():
                            self._option_token_meta[token] = meta

                else:
                    # All required new tokens were already represented
                    # or only stale tokens changed.
                    with self._lock:
                        self._option_tokens_by_symbol[symbol] = (
                            set(desired_tokens)
                        )

            except Exception as exc:
                self._option_last_error = (
                    f"{symbol}: {exc}"
                )
                logger.exception(
                    "Angel option token resolution failed: %s",
                    symbol,
                )

        self._option_last_refresh_at = time.time()

        logger.info(
            "Angel option websocket token map refreshed"
        )

    def _parse_option_tick(self, data: dict):
        token = str(
            data.get("token", "")
        ).strip()

        if not token:
            return

        with self._lock:
            meta = dict(
                self._option_token_meta.get(
                    token,
                    {},
                )
            )

        if not meta:
            logger.debug(
                "Unknown Angel option WebSocket token: %s",
                token,
            )
            return

        raw_ltp = data.get(
            "last_traded_price"
        )

        if not isinstance(raw_ltp, (int, float)):
            return

        exchange_ts = data.get(
            "exchange_timestamp"
        )

        timestamp = None
        if isinstance(exchange_ts, (int, float)):
            timestamp = epoch_ms_to_ist(
                exchange_ts
            ).isoformat()

        tick = {
            **meta,
            "ltp": float(raw_ltp) / 100.0,
            "raw_ltp": raw_ltp,
            "last_traded_quantity":
                data.get("last_traded_quantity"),
            "volume_trade_for_the_day":
                data.get("volume_trade_for_the_day"),
            "total_buy_quantity":
                data.get("total_buy_quantity"),
            "total_sell_quantity":
                data.get("total_sell_quantity"),
            "open_interest":
                data.get("open_interest"),
            "open_interest_change_percentage_raw":
                data.get(
                    "open_interest_change_percentage"
                ),

            # Angel SNAP_QUOTE best-5 prices are packet integers
            # in paise.  Keep the raw book too; expose top-of-book
            # as rupee values for downstream option calculations.
            "best_5_buy_data":
                data.get("best_5_buy_data") or [],
            "best_5_sell_data":
                data.get("best_5_sell_data") or [],
            "bidprice": (
                float((data.get("best_5_buy_data") or [{}])[0].get("price", 0))
                / 100.0
                if (data.get("best_5_buy_data") or [{}])[0].get("price") is not None
                else 0.0
            ),
            "askPrice": (
                float((data.get("best_5_sell_data") or [{}])[0].get("price", 0))
                / 100.0
                if (data.get("best_5_sell_data") or [{}])[0].get("price") is not None
                else 0.0
            ),

            "last_traded_timestamp":
                data.get("last_traded_timestamp"),
            "exchange_timestamp":
                exchange_ts,
            "timestamp":
                timestamp,
            "received_at":
                time.time(),
            "source":
                "angel_one_websocket",
        }

        with self._lock:
            self._option_ticks[token] = tick

    def _on_data(self, wsapp, data):
        try:
            logger.debug(
                "Angel WebSocket RAW DATA callback: %s",
                data,
            )

            token = str(data.get("token", ""))
            exchange_type = data.get("exchange_type")

            # OPTION_WS_PATCH_20260923
            if exchange_type in (
                self.OPTION_EXCHANGE_TYPE,
                self.BFO_OPTION_EXCHANGE_TYPE,
            ):
                self._parse_option_tick(data)
                return

            symbol = self._token_to_symbol.get(token)

            raw_ltp = data.get("last_traded_price")

            if not symbol:
                logger.warning("Unknown Angel WebSocket token: %s", token)
                return

            if not isinstance(raw_ltp, (int, float)):
                return

            ltp = float(raw_ltp) / 100.0

            exchange_ts = data.get("exchange_timestamp")

            timestamp = None
            if isinstance(exchange_ts, (int, float)):
                timestamp = epoch_ms_to_ist(exchange_ts).isoformat()

            tick = {
                "symbol": symbol,
                "token": token,
                "ltp": ltp,
                "raw_ltp": raw_ltp,
                "exchange_timestamp": exchange_ts,
                "timestamp": timestamp,
                "received_at": time.time(),
                "source": "angel_one_websocket",
            }

            with self._lock:
                self._ticks[symbol] = tick
                self._last_tick_received_at[symbol] = time.time()

                if isinstance(exchange_ts, (int, float)):
                    self._last_exchange_timestamp[symbol] = (
                        float(exchange_ts) / 1000.0
                    )

            # LOCAL_5M_CANDLE_BUILDER_20260923
            # Keep candle mutation outside the index tick lock.
            self._update_local_5m_candle(
                symbol,
                ltp,
                exchange_ts,
            )

        except Exception:
            logger.exception("Error processing Angel WebSocket tick")

    def _update_local_5m_candle(
        self,
        symbol: str,
        ltp: float,
        exchange_ts=None,
    ):
        """Build completed 5-minute OHLC candles from live LTP ticks."""

        if not isinstance(ltp, (int, float)):
            return

        price = float(ltp)
        if price <= 0:
            return

        if isinstance(exchange_ts, (int, float)):
            ts = float(exchange_ts) / 1000.0
        else:
            ts = time.time()

        # Floor to the NSE 5-minute bucket.
        bucket_ts = ts - (ts % 300)

        with self._lock:
            current = self._local_5m_current.get(symbol)

            # First tick for this symbol.
            if current is None:
                self._local_5m_current[symbol] = {
                    "symbol": symbol,
                    "timestamp": bucket_ts,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 0.0,
                    "source": "angel_one_websocket_ltp",
                }
                return

            current_bucket = float(current["timestamp"])

            # Same 5-minute bucket.
            if bucket_ts == current_bucket:
                current["high"] = max(
                    float(current["high"]),
                    price,
                )
                current["low"] = min(
                    float(current["low"]),
                    price,
                )
                current["close"] = price
                return

            # Old/out-of-order tick.
            if bucket_ts < current_bucket:
                return

            # New bucket: close the previous candle.
            completed = dict(current)
            completed["complete"] = True

            candles = self._local_5m_completed[symbol]
            candles.append(completed)

            if len(candles) > self._local_5m_max_candles:
                del candles[:-self._local_5m_max_candles]

            logger.info(
                "LOCAL 5M CANDLE CLOSED: %s "
                "bucket=%s O=%.2f H=%.2f L=%.2f C=%.2f",
                symbol,
                datetime.fromtimestamp(
                    current_bucket
                ).isoformat(),
                float(completed["open"]),
                float(completed["high"]),
                float(completed["low"]),
                float(completed["close"]),
            )

            # Start only the bucket for which we actually received a tick.
            # Do not manufacture missing buckets.
            self._local_5m_current[symbol] = {
                "symbol": symbol,
                "timestamp": bucket_ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0.0,
                "source": "angel_one_websocket_ltp",
            }

    def get_completed_5m_candles(
        self,
        symbol: str,
    ) -> List[dict]:
        """Return a snapshot of completed local 5-minute candles."""

        with self._lock:
            return [
                dict(candle)
                for candle in self._local_5m_completed.get(
                    symbol,
                    [],
                )
            ]

    def get_current_5m_candle(
        self,
        symbol: str,
    ) -> Optional[dict]:
        """Return the currently forming local 5-minute candle."""

        with self._lock:
            candle = self._local_5m_current.get(symbol)
            return dict(candle) if candle else None

    def _on_error(self, *args):
        self._connected.clear()

        self._last_error = str(args)

        logger.warning(
            "Angel WebSocket error: %s",
            args,
        )

    def _on_close(self, *args):
        self._connected.clear()

        logger.warning(
            "Angel WebSocket closed: %s",
            args,
        )

    def get_tick(self, symbol: str) -> Optional[dict]:
        symbol = symbol.upper()

        with self._lock:
            tick = self._ticks.get(symbol)

            if tick is None:
                return None

            return dict(tick)

    def get_snapshot(self) -> Dict[str, dict]:
        with self._lock:
            return {
                symbol: dict(tick)
                for symbol, tick in self._ticks.items()
            }

    # OPTION_WS_PATCH_20260923

    def get_option_tick(
        self,
        token: str,
    ) -> Optional[dict]:
        token = str(token).strip()

        with self._lock:
            tick = self._option_ticks.get(token)

            if tick is None:
                return None

            return dict(tick)

    def get_option_snapshot(
        self,
        symbol: Optional[str] = None,
    ) -> Dict[str, dict]:
        symbol_filter = (
            symbol.upper()
            if symbol
            else None
        )

        with self._lock:
            result = {}

            for token, tick in self._option_ticks.items():
                if (
                    symbol_filter is not None
                    and str(
                        tick.get("symbol", "")
                    ).upper()
                    != symbol_filter
                ):
                    continue

                result[token] = dict(tick)

            return result

    def status(self) -> dict:
        now = time.time()

        with self._lock:
            tick_count = len(self._ticks)
            tick_age_sec = {}
            exchange_age_sec = {}

            for symbol in self.SYMBOL_TOKENS:
                received_at = self._last_tick_received_at.get(symbol, 0.0)
                exchange_ts = self._last_exchange_timestamp.get(symbol, 0.0)

                tick_age_sec[symbol] = (
                    round(now - received_at, 1)
                    if received_at > 0 else None
                )
                exchange_age_sec[symbol] = (
                    round(now - exchange_ts, 1)
                    if exchange_ts > 0 else None
                )

        return {
            "running": bool(
                self._thread and self._thread.is_alive()
            ),
            "connected": self._connected.is_set(),
            "market_open": is_market_hours_ist(),
            "symbols_with_ticks": tick_count,
            "fresh_tick_symbols": [
                symbol
                for symbol in self.SYMBOL_TOKENS
                if tick_age_sec.get(symbol) is not None
                and tick_age_sec[symbol] <= self.LIVE_TICK_MAX_AGE_SEC
                and exchange_age_sec.get(symbol) is not None
                and exchange_age_sec[symbol] <= self.LIVE_TICK_MAX_AGE_SEC
            ],
            "tick_age_sec": tick_age_sec,
            "exchange_age_sec": exchange_age_sec,
            "watchdog_interval_sec": self.WATCHDOG_INTERVAL_SEC,
            "live_tick_max_age_sec": self.LIVE_TICK_MAX_AGE_SEC,
            "stale_tick_reconnect_sec": self.STALE_TICK_RECONNECT_SEC,
            "started_at": self._started_at,
            "last_error": self._last_error,
            "source": "angel_one_websocket",

            # LOCAL_5M_STATUS_DIAGNOSTIC_20260923
            "local_5m": {
                symbol: {
                    "current": (
                        dict(self._local_5m_current[symbol])
                        if symbol in self._local_5m_current
                        else None
                    ),
                    "completed_count": len(
                        self._local_5m_completed.get(symbol, [])
                    ),
                    "latest_completed": (
                        dict(self._local_5m_completed[symbol][-1])
                        if self._local_5m_completed.get(symbol)
                        else None
                    ),
                }
                for symbol in self.SYMBOL_TOKENS
            },

            # OPTION_WS_PATCH_20260923
            "option_stream": {
                "tick_count":
                    len(self._option_ticks),
                "tokens_by_symbol": {
                    symbol: len(tokens)
                    for symbol, tokens
                    in self._option_tokens_by_symbol.items()
                },
                "last_refresh_at":
                    self._option_last_refresh_at,
                "last_error":
                    self._option_last_error,
            },
        }


angel_live_feed = AngelLiveFeed()
