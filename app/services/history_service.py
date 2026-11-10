"""
Persistence layer for market snapshots and AI/technical analysis results.

BEFORE THIS FILE EXISTED: app/models.py defined MarketData, OptionData, and
AnalysisResult tables, and app/database.py created them on startup — but no
code anywhere ever wrote a row to them. Tables existed but stayed empty
forever. This module is what actually saves history, and the API routes now
call it.
"""
from typing import Dict, List, Optional
from datetime import datetime, timedelta, time as dt_time, timezone
import logging
import time

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import MarketData, OptionData, AnalysisResult, IntradayOHLC
from app.utils.helpers import now_utc_naive, is_market_hours_ist

logger = logging.getLogger(__name__)


async def save_market_snapshot(spot: Dict) -> None:
    """Persist a live spot-price observation using its source timestamp.

    The timestamp supplied by Angel One is the fetch/observation time. Preserve
    it instead of replacing it with DB-write time, so historical grading does
    not accidentally make an older observation look newer.
    """
    try:
        async with AsyncSessionLocal() as session:
            price = spot.get("price")
            if price is None:
                return

            raw_timestamp = spot.get("timestamp")
            observed_at = None

            if raw_timestamp:
                try:
                    parsed = datetime.fromisoformat(
                        str(raw_timestamp).replace("Z", "+00:00")
                    )
                    if parsed.tzinfo is not None:
                        from datetime import timezone
                        observed_at = parsed.astimezone(timezone.utc).replace(
                            tzinfo=None
                        )
                    else:
                        observed_at = parsed
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid spot timestamp for %s: %r; "
                        "falling back to UTC write time",
                        spot.get("symbol"),
                        raw_timestamp,
                    )

            if observed_at is None:
                observed_at = now_utc_naive()

            row = MarketData(
                symbol=str(spot.get("symbol", "")).upper(),
                price=float(price),
                timestamp=observed_at,
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to save market snapshot for {spot.get('symbol')}: {e}")



async def save_intraday_ohlc_snapshot(
    symbol: str,
    rows,
    data_source: str = "angel_one_intraday",
    open_by_timestamp=None,
) -> None:
    """
    Persist real completed 5-minute candles.

    `rows` uses the MarketAnalyzer internal shape:
        (timestamp, high, low, close, volume)

    Open is supplied separately when available from the REST candle stream.
    Duplicate symbol+timestamp rows are updated only when the candle values
    actually changed.
    """
    if not rows:
        return

    from zoneinfo import ZoneInfo

    symbol = str(symbol).upper().strip()
    ist = ZoneInfo("Asia/Kolkata")
    utc = timezone.utc

    def normalize_timestamp(value):
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ist)

        return parsed.astimezone(utc).replace(tzinfo=None)

    def normalize_open_map(mapping):
        out = {}
        if not isinstance(mapping, dict):
            return out

        for raw_ts, raw_open in mapping.items():
            if raw_open is None:
                continue
            try:
                out[normalize_timestamp(raw_ts)] = float(raw_open)
            except (TypeError, ValueError):
                continue
        return out

    try:
        normalized = []
        open_map = normalize_open_map(open_by_timestamp)

        for row in rows:
            if not isinstance(row, (tuple, list)) or len(row) < 5:
                continue

            try:
                ts = normalize_timestamp(row[0])
                high = float(row[1])
                low = float(row[2])
                close = float(row[3])
                volume = int(row[4] or 0)
            except (TypeError, ValueError, OverflowError):
                continue

            if high <= 0 or low <= 0 or close <= 0:
                continue

            normalized.append(
                (
                    ts,
                    open_map.get(ts),
                    high,
                    low,
                    close,
                    volume,
                )
            )

        if not normalized:
            return

        normalized.sort(key=lambda x: x[0])

        min_ts = normalized[0][0]
        max_ts = normalized[-1][0]

        async with AsyncSessionLocal() as session:
            existing_rows = (
                await session.execute(
                    select(IntradayOHLC).where(
                        IntradayOHLC.symbol == symbol,
                        IntradayOHLC.timestamp >= min_ts,
                        IntradayOHLC.timestamp <= max_ts,
                    )
                )
            ).scalars().all()

            existing = {
                row.timestamp: row
                for row in existing_rows
                if row.timestamp is not None
            }

            inserted = 0
            updated = 0

            for ts, open_price, high, low, close, volume in normalized:
                row = existing.get(ts)

                if row is None:
                    session.add(
                        IntradayOHLC(
                            symbol=symbol,
                            timestamp=ts,
                            open_price=open_price,
                            high=high,
                            low=low,
                            close=close,
                            volume=volume,
                            data_source=data_source,
                        )
                    )
                    inserted += 1
                    continue

                changed = False

                if open_price is not None and row.open_price != open_price:
                    row.open_price = open_price
                    changed = True

                if row.high != high:
                    row.high = high
                    changed = True

                if row.low != low:
                    row.low = low
                    changed = True

                if row.close != close:
                    row.close = close
                    changed = True

                if row.volume != volume:
                    row.volume = volume
                    changed = True

                if row.data_source != data_source:
                    row.data_source = data_source
                    changed = True

                if changed:
                    updated += 1

            if inserted or updated:
                await session.commit()

            logger.info(
                "Persisted intraday 5M candles: symbol=%s rows=%d "
                "inserted=%d updated=%d source=%s",
                symbol,
                len(normalized),
                inserted,
                updated,
                data_source,
            )

    except Exception as e:
        logger.error(
            "Failed to save intraday OHLC for %s: %s",
            symbol,
            e,
        )


async def get_latest_intraday_ohlc_before(
    symbol: str,
    cutoff: datetime,
    limit: int = 1200,
    include_data_source: bool = False,
):
    """
    Return the latest persisted real 5-minute candles before `cutoff`.

    Database timestamps are stored as UTC-naive values, matching the existing
    history tables. Returned timestamps are converted to Asia/Kolkata aware
    datetimes so MarketAnalyzer can reuse its existing session/bucket logic.
    """
    from zoneinfo import ZoneInfo

    symbol = str(symbol).upper().strip()

    try:
        requested_limit = max(1, min(int(limit), 1200))
    except (TypeError, ValueError):
        requested_limit = 1200

    cutoff_utc = cutoff
    if cutoff_utc.tzinfo is not None:
        cutoff_utc = cutoff_utc.astimezone(timezone.utc).replace(
            tzinfo=None
        )

    try:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(IntradayOHLC)
                .where(
                    IntradayOHLC.symbol == symbol,
                    IntradayOHLC.timestamp < cutoff_utc,
                )
                .order_by(IntradayOHLC.timestamp.desc())
                .limit(requested_limit)
            )

            db_rows = (await session.execute(stmt)).scalars().all()

        if not db_rows:
            return []

        ist = ZoneInfo("Asia/Kolkata")

        rows = []

        for row in reversed(db_rows):
            if not row.timestamp:
                continue

            ts = row.timestamp.replace(
                tzinfo=timezone.utc
            ).astimezone(ist)

            row_data = (
                ts,
                row.high,
                row.low,
                row.close,
                row.volume or 0,
            )
            if include_data_source:
                row_data = row_data + (row.data_source,)
            rows.append(row_data)

        return rows

    except Exception as e:
        logger.warning(
            "Failed to read persisted intraday OHLC for %s: %s",
            symbol,
            e,
        )
        return []


async def save_option_chain_snapshot(symbol: str, expiry: str, option_df) -> bool:
    """Persist one row per strike/side for the current option chain snapshot.
    Used to build the OI-change-over-time history that get_oi_change() needs
    (currently a placeholder — see production checklist)."""
    if option_df is None or option_df.empty:
        return False
    try:
        _t0 = time.perf_counter()
        async with AsyncSessionLocal() as session:
            # One canonical timestamp for the COMPLETE option-chain snapshot.
            # Do not create a new timestamp for every strike/side row.
            snapshot_ts = now_utc_naive()

            rows = []
            for _, r in option_df.iterrows():
                snap_ts = snapshot_ts
                rows.append(OptionData(
                    symbol=symbol, expiry=expiry, strike=float(r["strike"]), option_type="CE",
                    last_price=float(r["ce_ltp"]), volume=int(r["ce_volume"]),
                    open_interest=int(r["ce_oi"]), implied_volatility=float(r["ce_iv"]),
                    timestamp=snap_ts,
                ))
                rows.append(OptionData(
                    symbol=symbol, expiry=expiry, strike=float(r["strike"]), option_type="PE",
                    last_price=float(r["pe_ltp"]), volume=int(r["pe_volume"]),
                    open_interest=int(r["pe_oi"]), implied_volatility=float(r["pe_iv"]),
                    timestamp=snap_ts,
                ))
            session.add_all(rows)
            await session.commit()
            return True
    except Exception as e:
        logger.error(f"Failed to save option chain snapshot for {symbol}: {e}")
        return False


async def get_latest_market_snapshot_before(
    symbol: str,
    cutoff: datetime,
) -> Optional[Dict]:
    """Return the latest persisted market snapshot strictly before cutoff.

    Used by closed-market fallback so today's synthetic/after-hours rows
    can never be mistaken for the last valid trading-session observation.
    """
    symbol = str(symbol).upper().strip()

    try:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(MarketData)
                .where(
                    MarketData.symbol == symbol,
                    MarketData.timestamp < cutoff,
                )
                .order_by(MarketData.timestamp.desc())
                .limit(3000)
            )
            rows = (await session.execute(stmt)).scalars().all()

        row = None

        # MarketData timestamps are stored as naive UTC.
        # Convert each candidate to IST and accept only observations that
        # fall inside an actual NSE regular-session day/time. This prevents
        # the closed-market collector's weekend duplicate rows from being
        # selected as the "last trading session".
        for candidate in rows:
            if not candidate.timestamp:
                continue

            utc_dt = candidate.timestamp.replace(tzinfo=timezone.utc)
            ist_dt = utc_dt.astimezone(
                __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
            )

            if ist_dt.weekday() >= 5:
                continue

            session_probe = ist_dt.replace(
                hour=10,
                minute=0,
                second=0,
                microsecond=0,
            )

            if not is_market_hours_ist(session_probe):
                continue

            if not (
                dt_time(9, 15) <= ist_dt.time() <= dt_time(15, 30)
            ):
                continue

            row = candidate
            break

        if row is None:
            return None

        return {
            "symbol": row.symbol,
            "price": float(row.price),
            "timestamp": row.timestamp.isoformat() if row.timestamp else "",
            "market_open": False,
            "market_status_source": "history_db_closed_fallback",
            "data_source": "history_db_closed_fallback",
        }

    except Exception as e:
        logger.warning(
            "Failed to read latest market snapshot for %s: %s",
            symbol,
            e,
        )
        return None


async def get_latest_option_chain_snapshot_before(
    symbol: str,
    expiry: str,
    cutoff: datetime,
) -> Optional[Dict]:
    """Return the latest complete persisted option-chain snapshot before cutoff.

    OptionData stores one CE and one PE row per strike under one canonical
    timestamp. Rebuild the exact shape expected by OptionAnalyzer:
        strikePrice -> CE / PE
    No provider-style change-in-OI value is fabricated; it is set to zero
    because OptionData does not persist that field.
    """
    symbol = str(symbol).upper().strip()
    expiry = str(expiry or "").strip()

    try:
        async with AsyncSessionLocal() as session:
            ts_stmt = (
                select(OptionData.timestamp)
                .where(
                    OptionData.symbol == symbol,
                    OptionData.expiry == expiry,
                    OptionData.timestamp < cutoff,
                )
                .group_by(OptionData.timestamp)
                .order_by(OptionData.timestamp.desc())
                .limit(3000)
            )
            timestamps = (await session.execute(ts_stmt)).scalars().all()

            snapshot_ts = None

            for candidate_ts in timestamps:
                if not candidate_ts:
                    continue

                utc_dt = candidate_ts.replace(tzinfo=timezone.utc)
                ist_dt = utc_dt.astimezone(
                    __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
                )

                if ist_dt.weekday() >= 5:
                    continue

                session_probe = ist_dt.replace(
                    hour=10,
                    minute=0,
                    second=0,
                    microsecond=0,
                )

                if not is_market_hours_ist(session_probe):
                    continue

                if not (
                    dt_time(9, 15) <= ist_dt.time() <= dt_time(15, 30)
                ):
                    continue

                snapshot_ts = candidate_ts
                break

            if snapshot_ts is None:
                return None

            rows_stmt = (
                select(OptionData)
                .where(
                    OptionData.symbol == symbol,
                    OptionData.expiry == expiry,
                    OptionData.timestamp == snapshot_ts,
                )
                .order_by(
                    OptionData.strike,
                    OptionData.option_type,
                )
            )
            rows = (await session.execute(rows_stmt)).scalars().all()

        if not rows:
            return None

        grouped = {}

        for row in rows:
            strike = float(row.strike)
            side = str(row.option_type or "").upper()

            if side not in ("CE", "PE"):
                continue

            item = grouped.setdefault(
                strike,
                {
                    "strikePrice": strike,
                    "CE": {},
                    "PE": {},
                },
            )

            item[side] = {
                "lastPrice": float(row.last_price or 0),
                "openInterest": int(row.open_interest or 0),
                "totalTradedVolume": int(row.volume or 0),
                "impliedVolatility": float(row.implied_volatility or 0),
                "changeinOpenInterest": 0,
                "bidprice": 0,
                "askPrice": 0,
            }

        complete = [
            item
            for _, item in sorted(grouped.items())
            if item["CE"] and item["PE"]
        ]

        if not complete:
            return None

        return {
            "symbol": symbol,
            "expiry": expiry,
            "all_expiries": [expiry] if expiry else [],
            "underlying_price": 0.0,
            "data": complete,
            "data_source": "history_db_closed_fallback",
            "snapshot_timestamp": (
                snapshot_ts.isoformat()
                if snapshot_ts
                else ""
            ),
            "market_open": False,
        }

    except Exception as e:
        logger.warning(
            "Failed to read option-chain snapshot for %s %s: %s",
            symbol,
            expiry,
            e,
        )
        return None


async def get_oi_change_since(symbol: str, expiry: str, current_df, minutes_ago: int = 15) -> Dict:
    """
    Real OI-change-over-time: compares the CURRENT option-chain snapshot
    (current_df, already fetched this request) against the most recent
    snapshot this app itself saved at least `minutes_ago` minutes ago —
    not the provider's single "today's change" field, which only tells
    you the change since yesterday's close, not "what happened in the
    last 15 minutes".

    Classifies each strike/side using OI-delta + price-delta together
    (the standard convention):
      OI up   + price up   → Long Buildup
      OI up   + price down → Short Buildup
      OI down + price up   → Short Covering
      OI down + price down → Long Unwinding

    Returns {"available": False, ...} when there isn't yet a snapshot old
    enough to compare against (e.g. app just started) — callers should
    show "building history..." rather than a fabricated comparison.
    """
    if current_df is None or current_df.empty:
        return {"available": False, "reason": "no current option chain"}

    _t0 = time.perf_counter()
    cutoff = now_utc_naive() - timedelta(minutes=minutes_ago)
    async with AsyncSessionLocal() as session:
        stmt = (
            select(OptionData)
            .where(OptionData.symbol == symbol, OptionData.expiry == expiry, OptionData.timestamp <= cutoff)
            .order_by(OptionData.timestamp.desc())
            .limit(500)
        )
        rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        logger.info("DB timing: get_oi_change_since symbol=%s rows=0 elapsed=%.3fs", symbol, time.perf_counter() - _t0)
        return {"available": False, "reason": f"no snapshot older than {minutes_ago}m yet — history still building"}

    logger.info("DB timing: get_oi_change_since symbol=%s rows=%d elapsed=%.3fs", symbol, len(rows), time.perf_counter() - _t0)

    ref_ts = rows[0].timestamp
    snapshot_rows = [r for r in rows if ref_ts and r.timestamp and abs((ref_ts - r.timestamp).total_seconds()) <= 5]
    prev_map = {(r.strike, r.option_type): (r.open_interest or 0, r.last_price or 0) for r in snapshot_rows}

    actual_minutes = round((now_utc_naive() - ref_ts).total_seconds() / 60, 1) if ref_ts else minutes_ago

    ce_delta_total = 0
    pe_delta_total = 0
    per_strike: List[Dict] = []

    for _, row in current_df.iterrows():
        strike = float(row["strike"])
        for side, oi_col, price_col in (("CE", "ce_oi", "ce_ltp"), ("PE", "pe_oi", "pe_ltp")):
            prev = prev_map.get((strike, side))
            if prev is None:
                continue
            prev_oi, prev_price = prev
            cur_oi, cur_price = int(row[oi_col]), float(row[price_col])
            oi_delta = cur_oi - int(prev_oi)
            price_delta = cur_price - float(prev_price)
            if oi_delta > 0 and price_delta > 0:
                label = "Long Buildup"
            elif oi_delta > 0 and price_delta < 0:
                label = "Short Buildup"
            elif oi_delta < 0 and price_delta > 0:
                label = "Short Covering"
            elif oi_delta < 0 and price_delta < 0:
                label = "Long Unwinding"
            else:
                label = "Flat"
            if side == "CE":
                ce_delta_total += oi_delta
            else:
                pe_delta_total += oi_delta
            if oi_delta != 0:
                per_strike.append({
                    "strike": strike, "side": side, "oi_change": oi_delta,
                    "price_change": round(price_delta, 2), "label": label,
                })

    per_strike.sort(key=lambda x: abs(x["oi_change"]), reverse=True)

    return {
        "available": True,
        "window_minutes": actual_minutes,
        "ce_oi_change": ce_delta_total,
        "pe_oi_change": pe_delta_total,
        "top_buildups": per_strike[:8],
    }


async def save_analysis_result(symbol: str, analysis_type: str, result: Dict) -> None:
    """Persist an analysis result, avoiding duplicate writes for one market snapshot.

    AnalysisResult.timestamp is the DB write time, so it is not suitable for
    identifying the underlying market snapshot.  New AI results carry
    ``market_snapshot_timestamp`` from the source market overview.  We use that
    value for application-level deduplication while remaining compatible with
    both SQLite and PostgreSQL.
    """
    try:
        async with AsyncSessionLocal() as session:
            snapshot_timestamp = result.get("market_snapshot_timestamp")

            # AI history is sampled at roughly 5-minute intervals.  The live
            # analysis endpoint can be called much more frequently (the normal
            # market-data cache is ~45 seconds), so persist at most one AI row
            # per symbol/type within a 5-minute window.
            if analysis_type == "ai":
                latest_row = (
                    await session.execute(
                        select(AnalysisResult)
                        .where(AnalysisResult.symbol == symbol)
                        .where(AnalysisResult.analysis_type == analysis_type)
                        .order_by(AnalysisResult.timestamp.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                if latest_row and latest_row.timestamp:
                    age = now_utc_naive() - latest_row.timestamp
                    if age < timedelta(minutes=5):
                        logger.debug(
                            "Skipping %s analysis for %s "
                            "(last persisted %s ago; 5-minute history interval)",
                            analysis_type,
                            symbol,
                            age,
                        )
                        return

            # Exact market-snapshot dedupe remains as a second guard.
            if snapshot_timestamp:
                existing_rows = (
                    await session.execute(
                        select(AnalysisResult)
                        .where(AnalysisResult.symbol == symbol)
                        .where(AnalysisResult.analysis_type == analysis_type)
                        .order_by(AnalysisResult.timestamp.desc())
                        .limit(20)
                    )
                ).scalars().all()

                for existing in existing_rows:
                    existing_result = existing.result or {}
                    if (
                        existing_result.get("market_snapshot_timestamp")
                        == snapshot_timestamp
                    ):
                        logger.debug(
                            "Skipping duplicate %s analysis for %s "
                            "(market snapshot %s)",
                            analysis_type,
                            symbol,
                            snapshot_timestamp,
                        )
                        return

            row = AnalysisResult(
                symbol=symbol,
                analysis_type=analysis_type,
                result=result,
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        logger.error(
            f"Failed to save {analysis_type} analysis result for {symbol}: {e}"
        )
