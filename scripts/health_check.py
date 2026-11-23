#!/usr/bin/env python3
"""Independent four-symbol spot health probe.

Uses GET /api/market/spot/{symbol}.
Never treats REST/NSE fetch timestamps as exchange observation timestamps.
No broker credentials or third-party Python packages are required.
"""

import argparse
import json
import math
import os
import sys
import time

from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX")

WEBSOCKET_SOURCE = "angel_one_websocket"
REST_SOURCES = {"angel_one", "zerodha"}
NSE_SOURCE_PREFIX = "nse_curl_cffi/"


def _number(value):
    """Return a finite float, or None."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _valid_aware_timestamp(value):
    """Validate ISO timestamp syntax and require an explicit timezone."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _known_source(source):
    if source == WEBSOCKET_SOURCE or source in REST_SOURCES:
        return True
    return isinstance(source, str) and source.startswith(NSE_SOURCE_PREFIX)


def _failed(symbol, reason, http_status=None):
    return {
        "symbol": symbol,
        "overall_status": "FAIL",
        "api_status": "FAIL",
        "provider_status": "NOT_VERIFIED",
        "market_open": False,
        "market_open_known": False,
        "market_state": "UNKNOWN_DEFAULTED_FALSE",
        "data_source": None,
        "exchange_freshness": "UNKNOWN",
        "exchange_age_sec": None,
        "http_status": http_status,
        "error": reason,
    }


def evaluate_payload(symbol, payload, now_epoch=None):
    """Validate one spot response and assess provider/freshness separately."""
    now_epoch = time.time() if now_epoch is None else float(now_epoch)

    if not isinstance(payload, dict):
        return _failed(symbol, "response_is_not_json_object")

    errors = []

    actual_symbol = str(payload.get("symbol") or "").strip().upper()
    if actual_symbol != symbol.upper():
        errors.append("symbol_mismatch")

    price = _number(payload.get("price"))
    if price is None or price <= 0:
        errors.append("invalid_or_nonpositive_price")

    source = payload.get("data_source")
    if not isinstance(source, str) or not _known_source(source):
        errors.append("unknown_or_missing_data_source")

    if not _valid_aware_timestamp(payload.get("timestamp")):
        errors.append("missing_or_invalid_fetch_timestamp")

    raw_market_open = payload.get("market_open")
    market_open_known = isinstance(raw_market_open, bool)
    # Required safe default: unknown/missing is treated as not open.
    market_open = raw_market_open is True

    if not market_open_known:
        market_state = "UNKNOWN_DEFAULTED_FALSE"
    elif market_open:
        market_state = "OPEN"
    else:
        market_state = "CLOSED"

    freshness = "NOT_APPLICABLE"
    exchange_age = None

    if market_open and source == WEBSOCKET_SOURCE:
        exchange_ms = _number(payload.get("exchange_timestamp"))

        if exchange_ms is None or exchange_ms <= 0:
            freshness = "INVALID"
        else:
            # DataFetcher emits exchange_timestamp in milliseconds.
            exchange_age = now_epoch - (exchange_ms / 1000.0)

            if -5.0 <= exchange_age <= 10.0:
                freshness = "FRESH"
            else:
                freshness = "STALE"

    elif market_open:
        # A REST/NSE timestamp is fetch time, not independently verified
        # exchange observation time.
        freshness = "UNVERIFIED"

    provider_status = "FAIL" if errors else "PASS"

    if errors or freshness in {"STALE", "INVALID"}:
        overall = "FAIL"
    elif not market_open_known or freshness == "UNVERIFIED":
        overall = "WARN"
    else:
        overall = "PASS"

    return {
        "symbol": symbol,
        "overall_status": overall,
        "api_status": "PASS" if not errors else "FAIL",
        "provider_status": provider_status,
        "market_open": market_open,
        "market_open_known": market_open_known,
        "market_state": market_state,
        "data_source": source if isinstance(source, str) else None,
        "exchange_freshness": freshness,
        "exchange_age_sec": (
            round(exchange_age, 3) if exchange_age is not None else None
        ),
        "http_status": 200,
        "error": ",".join(errors) if errors else None,
    }


def probe_symbol(base_url, symbol, timeout=5.0, opener=None, now_epoch=None):
    """Perform one isolated HTTP request for one symbol."""
    opener = opener or urlopen
    url = (
        base_url.rstrip("/")
        + "/api/market/spot/"
        + quote(symbol.upper(), safe="")
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "NiftySpotHealthCheck/1.0",
        },
        method="GET",
    )

    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read()

        if status != 200:
            return _failed(symbol, "unexpected_http_status", status)

    except HTTPError as exc:
        return _failed(symbol, "http_error", exc.code)
    except URLError:
        return _failed(symbol, "url_or_connection_error")
    except TimeoutError:
        return _failed(symbol, "request_timeout")
    except Exception as exc:
        return _failed(symbol, "request_error:" + type(exc).__name__)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return _failed(symbol, "invalid_json_response", status)

    result = evaluate_payload(symbol, payload, now_epoch=now_epoch)
    result["http_status"] = status
    return result


def run_checks(base_url, symbols=SYMBOLS, timeout=5.0,
               opener=None, now_epoch=None):
    """Run each symbol independently; one failure cannot hide other results."""
    results = []

    for symbol in symbols:
        try:
            result = probe_symbol(
                base_url,
                symbol,
                timeout=timeout,
                opener=opener,
                now_epoch=now_epoch,
            )
        except Exception as exc:
            result = _failed(
                symbol, "probe_runner_error:" + type(exc).__name__
            )
        results.append(result)

    statuses = {item["overall_status"] for item in results}

    if "FAIL" in statuses:
        overall = "FAIL"
        exit_code = 2
    elif "WARN" in statuses:
        overall = "WARN"
        exit_code = 1
    else:
        overall = "PASS"
        exit_code = 0

    return {
        "overall_status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "symbols_checked": len(results),
        "results": results,
        "exit_code": exit_code,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Independent NIFTY-family spot API health check"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "NIFTY_HEALTH_BASE_URL", "http://127.0.0.1:8000"
        ),
        help="Application base URL; defaults to localhost:8000",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        choices=SYMBOLS,
        default=list(SYMBOLS),
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON"
    )

    args = parser.parse_args(argv)

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    report = run_checks(
        args.base_url,
        symbols=args.symbols,
        timeout=args.timeout,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("NIFTY SPOT HEALTH CHECK")
        print("Base URL:", args.base_url)
        print("-" * 105)
        print(
            f"{'SYMBOL':<12} {'OVERALL':<9} {'API':<7} "
            f"{'MARKET':<9} {'PROVIDER':<25} {'FRESHNESS':<19} AGE"
        )
        print("-" * 105)

        for item in report["results"]:
            age = item["exchange_age_sec"]
            age_text = f"{age:.3f}s" if age is not None else "--"
            print(
                f"{item['symbol']:<12} "
                f"{item['overall_status']:<9} "
                f"{item['api_status']:<7} "
                f"{item['market_state']:<9} "
                f"{str(item['data_source'] or '--'):<25} "
                f"{item['exchange_freshness']:<19} "
                f"{age_text}"
            )
            if item["error"]:
                print("  ERROR:", item["error"])

        print("-" * 105)
        print(
            f"OVERALL={report['overall_status']} "
            f"SYMBOLS_CHECKED={report['symbols_checked']}"
        )

    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
