import asyncio
import concurrent.futures
import logging
import socket
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

    def _on_error(self, wsapp, error):
        """Make SDK retry waits cancellable and stop retries during shutdown."""
        shutdown_event = getattr(self, "_shutdown_event", None)

        if shutdown_event is not None and shutdown_event.is_set():
            self.RESUBSCRIBE_FLAG = False
            self.DISCONNECT_FLAG = True
            self.close_connection()
            return

        self.RESUBSCRIBE_FLAG = True

        if self.current_retry_attempt < self.MAX_RETRY_ATTEMPT:
            logger.warning(
                "Attempting to resubscribe/reconnect (Attempt %s)...",
                self.current_retry_attempt + 1,
            )
            self.current_retry_attempt += 1

            if self.retry_strategy == 0:
                delay = self.retry_delay
            elif self.retry_strategy == 1:
                delay = self.retry_delay * (
                    self.retry_multiplier ** (self.current_retry_attempt - 1)
                )
            else:
                raise Exception(
                    f"Invalid retry strategy {self.retry_strategy}"
                )

            # Unlike SDK time.sleep(), this wait returns immediately on shutdown.
            if shutdown_event is not None:
                stopping = shutdown_event.wait(delay)
            else:
                time.sleep(delay)
                stopping = False

            if stopping or (
                shutdown_event is not None and shutdown_event.is_set()
            ):
                self.RESUBSCRIBE_FLAG = False
                self.DISCONNECT_FLAG = True
                self.close_connection()
                return

            try:
                self.close_connection()

                if (
                    shutdown_event is not None
                    and shutdown_event.is_set()
                ):
                    return

                self.connect()

            except Exception as exc:
                if (
                    shutdown_event is not None
                    and shutdown_event.is_set()
                ):
                    self.RESUBSCRIBE_FLAG = False
                    self.DISCONNECT_FLAG = True
                    self.close_connection()
                    return

                logger.error(
                    "Error occurred during resubscribe/reconnect: %s", exc
                )
                if hasattr(self, "on_error"):
                    self.on_error(
                        "Reconnect Error",
                        str(exc) if str(exc) else "Unknown error",
                    )
        else:
            self.close_connection()
            if hasattr(self, "on_error"):
                self.on_error("Max retry attempt reached", "Connection closed")

            if (
                self.retry_duration is not None
                and self.last_pong_timestamp is not None
                and time.time() - self.last_pong_timestamp
                > self.retry_duration * 60
            ):
                logger.warning("Connection closed due to inactivity.")
            else:
                logger.warning("Connection closed due to max retry attempts reached.")

    def connect(self):
        """Refuse an SDK reconnect once the owning feed starts shutting down."""
        shutdown_event = getattr(self, "_shutdown_event", None)
        if shutdown_event is not None and shutdown_event.is_set():
            self.RESUBSCRIBE_FLAG = False
            self.DISCONNECT_FLAG = True
            return

        super().connect()

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
        self._option_refresh_future_lock = threading.Lock()
        self._option_refresh_future = None
        self._option_refresh_completion = None
        self._option_refresh_started = None
        self._option_pending_sends_lock = threading.Lock()
        self._option_pending_sends = set()
        self._option_last_refresh_at: Optional[float] = None
        self._option_last_error: Optional[str] = None
        # OPTION_REFRESH_EVENTLOOP_FIX_20260923
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> bool:
        """Ensure Angel session exists, then start WS in a background thread."""

        if self._thread and self._thread.is_alive():
            if self._stop_event.is_set():
                logger.warning(
                    "Angel live feed start refused while previous feed is stopping"
                )
                return False
            return True

        if (
            self._option_refresh_thread
            and self._option_refresh_thread.is_alive()
        ):
            logger.warning(
                "Angel live feed start refused: previous option refresh worker is still alive"
            )
            return False

        if self._watchdog_thread and self._watchdog_thread.is_alive():
            logger.warning(
                "Angel live feed start refused: previous watchdog is still alive"
            )
            return False

        with self._option_refresh_future_lock:
            previous_completion = self._option_refresh_completion
            if previous_completion is not None and not previous_completion.is_set():
                logger.warning(
                    "Angel live feed start refused: previous refresh coroutine is still active"
                )
                return False
            if previous_completion is not None:
                self._option_refresh_completion = None
                self._option_refresh_started = None

        pending_lock = getattr(self, "_option_pending_sends_lock", None)
        if pending_lock is not None:
            with pending_lock:
                pending_sends = [
                    event for event in self._option_pending_sends
                    if not event.is_set()
                ]
            if pending_sends:
                logger.warning(
                    "Angel live feed start refused: previous option socket send is still active"
                )
                return False

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

    def stop(self) -> bool:
        """Stop Angel feed workers and report whether all threads terminated."""
        self._stop_event.set()
        self._option_refresh_stop.set()
        self._option_refresh_now.set()
        with self._lock:
            self._connected.clear()
        self._option_refresh_now.set()

        # Cancelling the coroutine alone cannot stop an already-running
        # asyncio.to_thread() socket send. Interrupt the underlying socket too.
        with self._option_refresh_future_lock:
            refresh_future = self._option_refresh_future
            refresh_completion = self._option_refresh_completion
        if refresh_future is not None:
            refresh_future.cancel()

        ws = self._ws
        if ws is not None:
            try:
                self._interrupt_websocket_socket(ws)
            except Exception:
                logger.exception("Error interrupting Angel WebSocket socket")
            try:
                ws.close_connection()
            except Exception:
                logger.exception("Error closing Angel WebSocket")

        threads = (
            ("option refresh", self._option_refresh_thread, 5),
            ("live feed", self._thread, 5),
            ("watchdog", self._watchdog_thread, 2),
        )
        for label, thread, timeout in threads:
            if thread and thread.is_alive():
                if thread is threading.current_thread():
                    logger.warning(
                        "Angel %s thread cannot join itself during shutdown", label
                    )
                    continue
                thread.join(timeout=timeout)

        refresh_coroutine_alive = False
        if refresh_completion is not None:
            refresh_completion.wait(timeout=5.0)
            refresh_coroutine_alive = not refresh_completion.is_set()

        # The concurrent future can be cancelled before its to_thread()
        # socket operation has actually returned. Wait for those operations.
        send_deadline = time.monotonic() + 5.0
        with self._option_pending_sends_lock:
            pending_snapshot = tuple(self._option_pending_sends)

        for send_done in pending_snapshot:
            send_done.wait(
                timeout=max(0.0, send_deadline - time.monotonic())
            )

        with self._option_pending_sends_lock:
            for send_done in tuple(self._option_pending_sends):
                if send_done.is_set():
                    self._option_pending_sends.discard(send_done)
            pending_sends_alive = any(
                not send_done.is_set()
                for send_done in self._option_pending_sends
            )

        option_alive = bool(
            self._option_refresh_thread
            and self._option_refresh_thread.is_alive()
        )
        feed_alive = bool(self._thread and self._thread.is_alive())
        watchdog_alive = bool(
            self._watchdog_thread and self._watchdog_thread.is_alive()
        )

        if not option_alive:
            self._option_refresh_thread = None
        if (
            not option_alive
            and not pending_sends_alive
            and not refresh_coroutine_alive
        ):
            with self._option_refresh_future_lock:
                if self._option_refresh_future is refresh_future:
                    self._option_refresh_future = None
                if self._option_refresh_completion is refresh_completion:
                    self._option_refresh_completion = None
                    self._option_refresh_started = None
        if not feed_alive:
            self._thread = None
        if not watchdog_alive:
            self._watchdog_thread = None

        all_stopped = not (
            option_alive
            or feed_alive
            or watchdog_alive
            or pending_sends_alive
            or refresh_coroutine_alive
        )
        if all_stopped:
            self._ws = None
            self._async_loop = None
            logger.info("Angel live feed and all workers stopped")
        else:
            still_alive = []
            if option_alive:
                still_alive.append("option-refresh")
            if feed_alive:
                still_alive.append("live-feed")
            if watchdog_alive:
                still_alive.append("watchdog")
            if pending_sends_alive:
                still_alive.append("option-socket-send")
            if refresh_coroutine_alive:
                still_alive.append("option-refresh-coroutine")
            logger.warning(
                "Angel live feed shutdown incomplete; threads still alive: %s",
                ", ".join(still_alive),
            )

        return all_stopped

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

                ws = _CompatibleSmartWebSocketV2(
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
                # The SDK callback uses this event to abort retries during shutdown.
                ws._shutdown_event = self._stop_event
                self._ws = ws

                if self._stop_event.is_set():
                    try:
                        ws.close_connection()
                    except Exception:
                        logger.exception(
                            "Angel WebSocket close failed during startup/shutdown race"
                        )
                    break

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
        # Shutdown may race with SDK connection initialization. If the
        # connection finishes after stop() has begun, never subscribe it.
        if self._stop_event.is_set():
            ws = self._ws
            if ws is not None:
                self._close_stopping_websocket(ws)
            return

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
                stopping = self._stop_event.is_set()
                if not stopping:
                    self._last_tick_received_at.clear()
                    self._last_exchange_timestamp.clear()

                    # OPTION_WS_PATCH_20260923
                    # Token mappings belong to this socket connection.
                    self._option_ticks.clear()
                    self._option_token_meta.clear()
                    self._option_tokens_by_symbol.clear()

                    self._connected.set()
                    self._option_refresh_now.set()

            if stopping:
                self._close_stopping_websocket(self._ws)
                return

            logger.info(
                "Angel WebSocket subscribed: "
                "NIFTY/BANKNIFTY/FINNIFTY(NSE) + SENSEX(BSE)"
            )

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

            future = None
            completion_event = threading.Event()
            started_event = threading.Event()

            try:
                loop = self._async_loop
                if loop is None or loop.is_closed():
                    raise RuntimeError(
                        "Angel option refresh event loop is unavailable"
                    )

                with self._option_refresh_future_lock:
                    self._option_refresh_completion = completion_event
                    self._option_refresh_started = started_event
                    cancel_now = self._option_refresh_stop.is_set()

                if cancel_now:
                    completion_event.set()
                    break

                future = asyncio.run_coroutine_threadsafe(
                    self._run_option_refresh_tracked(
                        started_event,
                        completion_event,
                    ),
                    loop,
                )

                with self._option_refresh_future_lock:
                    self._option_refresh_future = future
                    cancel_now = self._option_refresh_stop.is_set()

                if cancel_now:
                    future.cancel()

                future.result()

            except concurrent.futures.CancelledError:
                if not self._option_refresh_stop.is_set():
                    self._option_last_error = "Angel option refresh future cancelled"
                    logger.warning(self._option_last_error)
            except Exception as exc:
                if not self._option_refresh_stop.is_set():
                    self._option_last_error = str(exc)
                    logger.exception(
                        "Angel option websocket refresh failed"
                    )
            finally:
                # If cancellation happened before the coroutine began,
                # there is no coroutine left whose finally block can signal.
                if future is None or (
                    future.cancelled() and not started_event.is_set()
                ):
                    completion_event.set()

                with self._option_refresh_future_lock:
                    if self._option_refresh_future is future and future is not None:
                        self._option_refresh_future = None

                    if (
                        completion_event.is_set()
                        and self._option_refresh_completion is completion_event
                    ):
                        self._option_refresh_completion = None
                        self._option_refresh_started = None

            self._option_refresh_now.wait(
                self.OPTION_REFRESH_INTERVAL_SEC
            )
            self._option_refresh_now.clear()

    @staticmethod
    def _interrupt_websocket_socket(ws) -> None:
        """Interrupt a blocked SDK socket send before asking the SDK to close."""
        wsapp = getattr(ws, "wsapp", None)
        protocol_socket = getattr(wsapp, "sock", None)
        raw_socket = getattr(protocol_socket, "sock", None)
        if raw_socket is None:
            return

        try:
            # socket.close() alone does not reliably interrupt another thread's
            # blocked send(). SHUT_RDWR wakes that operation so SDK cleanup can
            # acquire its send lock and finish.
            raw_socket.shutdown(socket.SHUT_RDWR)
        except OSError as exc:
            logger.debug(
                "Angel WebSocket socket shutdown was already complete: %s", exc
            )

    def _close_stopping_websocket(self, ws):
        """Interrupt and close a late-opened socket on its worker thread."""
        try:
            self._interrupt_websocket_socket(ws)
            ws.close_connection()
        except Exception:
            logger.exception("Angel WebSocket late-open shutdown failed")

    async def _run_option_refresh_tracked(self, started_event, completion_event):
        started_event.set()
        try:
            await self._refresh_option_subscriptions()
        finally:
            completion_event.set()

    async def _run_option_ws_operation(self, operation, *args):
        """Track a blocking WebSocket send until its worker thread exits."""
        send_done = threading.Event()
        with self._option_pending_sends_lock:
            if self._stop_event.is_set() or self._option_refresh_stop.is_set():
                raise asyncio.CancelledError()
            self._option_pending_sends.add(send_done)

        started = threading.Event()
        abandoned = threading.Event()

        def _invoke():
            started.set()
            try:
                if (
                    abandoned.is_set()
                    or self._stop_event.is_set()
                    or self._option_refresh_stop.is_set()
                ):
                    return None
                return operation(*args)
            finally:
                send_done.set()
                with self._option_pending_sends_lock:
                    self._option_pending_sends.discard(send_done)

        try:
            result = await asyncio.to_thread(_invoke)
        except BaseException:
            abandoned.set()
            if not started.is_set():
                send_done.set()
                with self._option_pending_sends_lock:
                    self._option_pending_sends.discard(send_done)
            raise

        if self._stop_event.is_set() or self._option_refresh_stop.is_set():
            raise asyncio.CancelledError()
        return result

    async def _refresh_option_subscriptions(self):
        for symbol in self.SYMBOL_TOKENS:
            try:
                # LIVEFEED_TOKEN_METADATA_ONLY_20261008
                # Token discovery must not fetch FULL option-chain quotes.
                # The WebSocket itself supplies live option ticks.
                spot_price = None
                try:
                    tick = self.get_tick(symbol)
                    if isinstance(tick, dict):
                        raw_ltp = tick.get("ltp")
                        if raw_ltp is not None:
                            value = float(raw_ltp)
                            if value > 0:
                                spot_price = value
                except Exception:
                    spot_price = None

                metadata = await angel_session.resolve_option_tokens(
                    symbol,
                    strikes_each_side=10,
                    spot_price=spot_price,
                )

                desired_meta = {
                    str(item["token"]).strip(): item
                    for item in metadata
                    if isinstance(item, dict)
                    and item.get("token")
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

                stale_tokens = current_tokens - desired_tokens
                new_tokens = desired_tokens - current_tokens

                # Keep local tracking aligned with subscription operations
                # that completed without exceptions. Failed unsubscriptions
                # remain tracked so the next refresh can retry them.
                active_tokens = set(current_tokens)

                if stale_tokens:
                    try:
                        await self._run_option_ws_operation(
                            self._ws.unsubscribe,
                            "nifty-live-options",
                            self.OPTION_MODE_SNAP_QUOTE,
                            [{
                                "exchangeType": option_exchange_type,
                                "tokens": sorted(stale_tokens),
                            }],
                        )

                        with self._lock:
                            for token in stale_tokens:
                                self._option_token_meta.pop(token, None)
                                self._option_ticks.pop(token, None)

                        active_tokens.difference_update(stale_tokens)

                        logger.info(
                            "Angel option websocket unsubscribed: "
                            "%s tokens=%s",
                            symbol,
                            len(stale_tokens),
                        )

                    except Exception:
                        logger.exception(
                            "Angel option websocket unsubscribe failed: %s",
                            symbol,
                        )

                if new_tokens:
                    try:
                        await self._run_option_ws_operation(
                            self._ws.subscribe,
                            "nifty-live-options",
                            self.OPTION_MODE_SNAP_QUOTE,
                            [{
                                "exchangeType": option_exchange_type,
                                "tokens": sorted(new_tokens),
                            }],
                        )

                        active_tokens.update(new_tokens)

                        logger.info(
                            "Angel option websocket subscribed: "
                            "%s tokens=%s mode=SNAP_QUOTE exchange=%s",
                            symbol,
                            len(new_tokens),
                            "BFO" if symbol == "SENSEX" else "NFO",
                        )

                    except Exception:
                        logger.exception(
                            "Angel option websocket subscribe failed: "
                            "%s tokens=%s",
                            symbol,
                            len(new_tokens),
                        )

                # A successful unsubscribe removes stale tokens even if the
                # subsequent subscribe fails. A failed unsubscribe preserves
                # the old tokens and metadata for retries and incoming ticks.
                with self._lock:
                    self._option_tokens_by_symbol[symbol] = set(active_tokens)

                    # Refresh metadata only for desired tokens that are
                    # currently tracked; do not invent metadata for failed
                    # new subscriptions or overwrite metadata for stale tokens.
                    for token in desired_tokens.intersection(active_tokens):
                        self._option_token_meta[token] = desired_meta[token]

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

    def _option_stream_status(self, now=None) -> dict:
        """Return per-symbol option subscription and live-tick diagnostics."""
        current = time.time() if now is None else float(now)
        result = {}

        with self._lock:
            for symbol in self.SYMBOL_TOKENS:
                tokens = self._option_tokens_by_symbol.get(symbol, set())
                symbol_ticks = [
                    tick
                    for tick in self._option_ticks.values()
                    if str(tick.get("symbol", "")).upper() == symbol
                ]
                latest = max(
                    symbol_ticks,
                    key=lambda tick: float(tick.get("received_at") or 0.0),
                    default=None,
                )

                received_age = None
                exchange_age = None
                if latest is not None:
                    received_at = latest.get("received_at")
                    exchange_ts = latest.get("exchange_timestamp")
                    if isinstance(received_at, (int, float)) and received_at > 0:
                        received_age = round(current - float(received_at), 1)
                    if isinstance(exchange_ts, (int, float)) and exchange_ts > 0:
                        exchange_age = round(current - (float(exchange_ts) / 1000.0), 1)

                fresh = (
                    received_age is not None
                    and exchange_age is not None
                    and 0 <= received_age <= self.LIVE_TICK_MAX_AGE_SEC
                    and 0 <= exchange_age <= self.LIVE_TICK_MAX_AGE_SEC
                )
                result[symbol] = {
                    "subscribed_tokens": len(tokens),
                    "tick_count": len(symbol_ticks),
                    "latest_tick_age_sec": received_age,
                    "latest_exchange_age_sec": exchange_age,
                    "fresh": fresh,
                }

        return result
    def status(self) -> dict:
        now = time.time()

        with self._lock:
            tick_count = len(self._ticks)
            tick_age_sec = {}
            exchange_age_sec = {}
            symbol_status = {}

            for symbol in self.SYMBOL_TOKENS:
                received_at = self._last_tick_received_at.get(symbol, 0.0)
                exchange_ts = self._last_exchange_timestamp.get(symbol, 0.0)
                received_age = now - received_at if received_at > 0 else None
                exchange_age = now - exchange_ts if exchange_ts > 0 else None
                fresh = (
                    received_age is not None
                    and exchange_age is not None
                    and 0 <= received_age <= self.LIVE_TICK_MAX_AGE_SEC
                    and 0 <= exchange_age <= self.LIVE_TICK_MAX_AGE_SEC
                )

                tick_age_sec[symbol] = (
                    round(received_age, 1) if received_age is not None else None
                )
                exchange_age_sec[symbol] = (
                    round(exchange_age, 1) if exchange_age is not None else None
                )
                symbol_status[symbol] = {
                    "has_tick": symbol in self._ticks,
                    "fresh": fresh,
                    "tick_age_sec": tick_age_sec[symbol],
                    "exchange_age_sec": exchange_age_sec[symbol],
                }

        return {
            "running": bool(
                self._thread and self._thread.is_alive()
            ),
            "connected": self._connected.is_set(),
            "market_open": is_market_hours_ist(),
            "symbols_with_ticks": tick_count,
            "fresh_tick_symbols": [
                symbol
                for symbol, details in symbol_status.items()
                if details["fresh"]
            ],
            "symbol_status": symbol_status,
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
                "tick_count": len(self._option_ticks),
                "tokens_by_symbol": {
                    symbol: len(tokens)
                    for symbol, tokens in self._option_tokens_by_symbol.items()
                },
                "by_symbol": self._option_stream_status(now),
                "last_refresh_at": self._option_last_refresh_at,
                "last_error": self._option_last_error,
            },
        }


angel_live_feed = AngelLiveFeed()
