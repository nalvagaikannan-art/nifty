"""
Persistence layer for market snapshots and AI/technical analysis results.

BEFORE THIS FILE EXISTED: app/models.py defined MarketData, OptionData, and
AnalysisResult tables, and app/database.py created them on startup — but no
code anywhere ever wrote a row to them. Tables existed but stayed empty
forever. This module is what actually saves history, and the API routes now
call it.
"""
from app.utils.expiry import canonical_expiry, expiry_matches
from typing import Dict, List, Optional
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo
import asyncio
import logging
import time

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import MarketData, OptionData, AnalysisResult, IntradayOHLC
from app.utils.helpers import now_utc_naive, is_market_hours_ist

logger = logging.getLogger(__name__)

# Serialize AnalysisResult check-and-write operations within this app process.
# The deployed service runs a single Python process/event loop.
_ANALYSIS_RESULT_SAVE_LOCKS = {}


def _analysis_result_save_lock():
    loop = asyncio.get_running_loop()
    lock = _ANALYSIS_RESULT_SAVE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _ANALYSIS_RESULT_SAVE_LOCKS[loop] = lock
    return lock


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
    expiry = canonical_expiry(expiry)
    if not expiry:
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
                    expiry_matches(OptionData.expiry, expiry),
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
                    expiry_matches(OptionData.expiry, expiry),
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




async def get_latest_option_chain_snapshot_for_nearest_expiry_before(
    symbol: str,
    cutoff: datetime,
) -> Optional[Dict]:
    """Return the nearest future expiry with a reconstructable persisted chain."""
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return None

    try:
        if cutoff.tzinfo is None:
            cutoff_utc = cutoff
        else:
            cutoff_utc = cutoff.astimezone(timezone.utc).replace(tzinfo=None)

        cutoff_ist_date = cutoff_utc.replace(
            tzinfo=timezone.utc
        ).astimezone(ZoneInfo("Asia/Kolkata")).date()

        async with AsyncSessionLocal() as session:
            expiry_stmt = (
                select(OptionData.expiry)
                .where(
                    OptionData.symbol == symbol,
                    OptionData.timestamp < cutoff_utc,
                )
                .distinct()
            )
            raw_expiries = (
                await session.execute(expiry_stmt)
            ).scalars().all()

        # Normalize legacy expiry spellings and discard expired/invalid dates.
        candidates_by_date = {}
        for raw_expiry in raw_expiries:
            canonical = canonical_expiry(raw_expiry)
            try:
                expiry_date = datetime.strptime(
                    canonical, "%d%b%Y"
                ).date()
            except (TypeError, ValueError):
                continue

            if expiry_date < cutoff_ist_date:
                continue
            candidates_by_date[expiry_date] = canonical

        if not candidates_by_date:
            return None

        valid_expiries = []
        selected_chain = None

        for expiry_date in sorted(candidates_by_date):
            expiry_key = candidates_by_date[expiry_date]
            snapshot = await get_latest_option_chain_snapshot_before(
                symbol,
                expiry_key,
                cutoff_utc,
            )

            if not isinstance(snapshot, dict) or not snapshot.get("data"):
                continue

            valid_expiries.append(expiry_key)

            if selected_chain is None:
                selected_chain = dict(snapshot)
                selected_chain["expiry"] = canonical_expiry(
                    snapshot.get("expiry") or expiry_key
                )

        if selected_chain is None:
            return None

        selected_chain["all_expiries"] = valid_expiries
        return selected_chain

    except Exception as exc:
        logger.warning(
            "Failed to discover persisted option-chain expiries for %s: %s",
            symbol,
            exc,
        )
        return None

def _classify_oi_wall_change(oi_delta: int, price_delta: float) -> str:
    """Classify OI and premium changes at one specific strike."""
    if oi_delta > 0 and price_delta > 0:
        return "Long Buildup"
    if oi_delta > 0 and price_delta < 0:
        return "Short Buildup"
    if oi_delta < 0 and price_delta > 0:
        return "Short Covering"
    if oi_delta < 0 and price_delta < 0:
        return "Long Unwinding"
    return "Flat"


def _build_oi_wall_evidence(current_df, prev_map) -> Dict:
    """Build tracked OI/price evidence for the current maximum-OI CE and PE walls.

    The caller supplies prev_map from the already time-guarded historical
    snapshot. This function performs no database reads. Downstream scoring
    validates the matching wall and price fields before using evidence.
    """
    import math

    result = {}

    if current_df is None or current_df.empty:
        return {
            "available": False,
            "ce": {"available": False, "reason": "no_current_chain"},
            "pe": {"available": False, "reason": "no_current_chain"},
        }

    for side, oi_col, price_col, key in (
        ("CE", "ce_oi", "ce_ltp", "ce"),
        ("PE", "pe_oi", "pe_ltp", "pe"),
    ):
        required = {"strike", oi_col, price_col}
        if not required.issubset(current_df.columns):
            result[key] = {
                "available": False,
                "reason": "required_columns_missing",
            }
            continue

        try:
            idx = current_df[oi_col].idxmax()
            row = current_df.loc[idx]
            strike = float(row["strike"])
            current_oi = int(float(row[oi_col]))
            current_price = float(row[price_col])
        except (TypeError, ValueError, KeyError, IndexError):
            result[key] = {
                "available": False,
                "reason": "invalid_current_wall_data",
            }
            continue
        except Exception:
            result[key] = {
                "available": False,
                "reason": "current_wall_lookup_failed",
            }
            continue

        if not math.isfinite(current_price) or current_price <= 0:
            result[key] = {"available": False, "strike": strike,
                           "reason": "invalid_current_wall_price"}
            continue

        previous = prev_map.get((strike, side))
        if previous is None:
            result[key] = {
                "available": False,
                "strike": strike,
                "reason": "wall_strike_missing_from_baseline",
            }
            continue

        try:
            previous_oi = int(float(previous[0] or 0))
            previous_price = float(previous[1] or 0)
        except (TypeError, ValueError, IndexError):
            result[key] = {
                "available": False,
                "strike": strike,
                "reason": "invalid_baseline_data",
            }
            continue

        if not math.isfinite(previous_price) or previous_price <= 0:
            result[key] = {"available": False, "strike": strike,
                           "reason": "invalid_baseline_price"}
            continue

        oi_delta = current_oi - previous_oi
        price_delta = current_price - previous_price

        result[key] = {
            "available": True,
            "strike": strike,
            "current_oi": current_oi,
            "previous_oi": previous_oi,
            "oi_change": oi_delta,
            "current_price": round(current_price, 2),
            "previous_price": round(previous_price, 2),
            "price_change": round(price_delta, 2),
            "label": _classify_oi_wall_change(oi_delta, price_delta),
        }

    return {
        "available": any(item.get("available", False)
                         for item in result.values()),
        "ce": result.get("ce", {
            "available": False, "reason": "not_calculated"
        }),
        "pe": result.get("pe", {
            "available": False, "reason": "not_calculated"
        }),
    }

async def get_oi_change_since(symbol: str, expiry: str, current_df, minutes_ago: int = 15, underlying: float = 0.0) -> Dict:
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

    # Stored OptionData timestamps are naive UTC. Restrict the reference
    # snapshot to today's NSE session, whose start is 09:15 Asia/Kolkata.
    now_utc = now_utc_naive()
    ist_tz = ZoneInfo("Asia/Kolkata")
    now_ist = now_utc.replace(tzinfo=timezone.utc).astimezone(ist_tz)
    session_start_ist = datetime.combine(
        now_ist.date(),
        dt_time(hour=9, minute=15),
        tzinfo=ist_tz,
    )
    session_start_utc = (
        session_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    )
    cutoff = now_utc - timedelta(minutes=minutes_ago)

    if cutoff < session_start_utc:
        return {
            "available": False,
            "reason": (
                f"no same-session snapshot older than {minutes_ago}m yet "
                "? history still building"
            ),
        }
    async with AsyncSessionLocal() as session:
        stmt = (
            select(OptionData)
            .where(
                OptionData.symbol == symbol,
                expiry_matches(OptionData.expiry, expiry),
                OptionData.timestamp >= session_start_utc,
                OptionData.timestamp <= cutoff,
            )
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

    # Optional ATM evidence uses the same eligible OI-history reference rows
    # and the same current_df prices as the per-strike OI/price classifier.
    import math
    try:
        underlying_value = float(underlying or 0.0)
    except (TypeError, ValueError):
        underlying_value = 0.0

    atm_strikes = set()
    if math.isfinite(underlying_value) and underlying_value > 0:
        current_strikes = [float(value) for value in current_df["strike"].tolist()]
        atm_position = min(
            range(len(current_strikes)),
            key=lambda pos: abs(current_strikes[pos] - underlying_value),
        )
        atm_strikes = set(
            current_strikes[max(0, atm_position - 1):min(len(current_strikes), atm_position + 2)]
        )

    ce_delta_total = 0
    pe_delta_total = 0
    per_strike: List[Dict] = []
    atm_contract_changes: List[Dict] = []

    # A reference timestamp alone does not guarantee sufficient chain
    # coverage. Track matched strikes on each side before exposing deltas.
    expected_chain_strikes = int(len(current_df))
    matched_chain_by_side = {"CE": 0, "PE": 0}

    for _, row in current_df.iterrows():
        strike = float(row["strike"])
        for side, oi_col, price_col in (("CE", "ce_oi", "ce_ltp"), ("PE", "pe_oi", "pe_ltp")):
            prev = prev_map.get((strike, side))
            if prev is None:
                continue
            matched_chain_by_side[side] += 1
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
            contract_change = {
                "strike": strike,
                "side": side,
                "oi_change": oi_delta,
                "price_change": round(price_delta, 2),
                "label": label,
            }
            if strike in atm_strikes:
                # Keep zero-OI-change contracts here too so ATM coverage is
                # explicit; the regular top_buildups list remains unchanged.
                atm_contract_changes.append(contract_change)

            if side == "CE":
                ce_delta_total += oi_delta
            else:
                pe_delta_total += oi_delta
            if oi_delta != 0:
                per_strike.append(contract_change)

    per_strike.sort(key=lambda x: abs(x["oi_change"]), reverse=True)

    expected_per_side = len(atm_strikes)
    atm_ce = [item for item in atm_contract_changes if item["side"] == "CE"]
    atm_pe = [item for item in atm_contract_changes if item["side"] == "PE"]
    ce_complete = expected_per_side > 0 and len(atm_ce) == expected_per_side
    pe_complete = expected_per_side > 0 and len(atm_pe) == expected_per_side
    atm_available = ce_complete and pe_complete

    atm_evidence = {
        "available": atm_available,
        "status": "available" if atm_available else "partial_or_underlying_missing",
        "reason": (
            None if atm_available else
            "ATM reference coverage incomplete or underlying price unavailable"
        ),
        "underlying": underlying_value if underlying_value > 0 else None,
        "atm_strikes": sorted(atm_strikes),
        "expected_contracts_per_side": expected_per_side,
        "ce_contracts": len(atm_ce),
        "pe_contracts": len(atm_pe),
        "ce_oi_change": sum(item["oi_change"] for item in atm_ce) if ce_complete else None,
        "pe_oi_change": sum(item["oi_change"] for item in atm_pe) if pe_complete else None,
        "ce_price_change": (
            round(sum(item["price_change"] for item in atm_ce) / len(atm_ce), 2)
            if ce_complete else None
        ),
        "pe_price_change": (
            round(sum(item["price_change"] for item in atm_pe) / len(atm_pe), 2)
            if pe_complete else None
        ),
        "window_minutes": actual_minutes,
        "data_source": "option_data_same_session_reference",
    }

    # Require at least 70% reference-row coverage on BOTH sides before
    # treating aggregate OI changes as a reliable chain-wide measurement.
    # A genuinely zero delta remains valid when reference coverage is good.
    minimum_coverage_pct = 70.0
    ce_coverage_pct = round(
        matched_chain_by_side["CE"] / expected_chain_strikes * 100, 1
    ) if expected_chain_strikes else 0.0
    pe_coverage_pct = round(
        matched_chain_by_side["PE"] / expected_chain_strikes * 100, 1
    ) if expected_chain_strikes else 0.0

    tracking_available = (
        expected_chain_strikes > 0
        and ce_coverage_pct >= minimum_coverage_pct
        and pe_coverage_pct >= minimum_coverage_pct
    )
    coverage = {
        "expected_contracts_per_side": expected_chain_strikes,
        "ce_matched_contracts": matched_chain_by_side["CE"],
        "pe_matched_contracts": matched_chain_by_side["PE"],
        "ce_coverage_pct": ce_coverage_pct,
        "pe_coverage_pct": pe_coverage_pct,
        "minimum_coverage_pct": minimum_coverage_pct,
    }
    unavailable_reason = (
        None if tracking_available else
        "Reference OI coverage below "
        f"{minimum_coverage_pct:.0f}% on at least one side "
        f"(CE {ce_coverage_pct:.1f}%, PE {pe_coverage_pct:.1f}%); "
        "history comparison is not reliable"
    )

    return {
        "available": tracking_available,
        "status": "available" if tracking_available else "unavailable",
        "reason": unavailable_reason,
        "coverage": coverage,
        "window_minutes": actual_minutes,
        "ce_oi_change": ce_delta_total if tracking_available else None,
        "pe_oi_change": pe_delta_total if tracking_available else None,
        "top_buildups": per_strike[:8] if tracking_available else [],
        "atm_evidence": atm_evidence,
        "wall_evidence": _build_oi_wall_evidence(current_df, prev_map),
    }



async def get_option_price_change_since(
    symbol: str,
    expiry: str,
    current_chain: Dict,
    minutes_ago: int = 15,
    max_reference_age_minutes: int = 30,
) -> Dict:
    """Compare same-contract option LTPs with an eligible quote-history snapshot.

    This reader is intentionally independent of OI scoring. A contract is
    compared only when its current and historical quote sources match. Current
    WebSocket quotes must be fresh and have a valid bid/ask; otherwise the
    current REST/provider quote is used with its normalized source identity.
    """
    import math
    import time as _time
    from app.models import OptionQuoteHistory

    symbol = str(symbol or "").upper().strip()
    expiry = canonical_expiry(expiry)
    if (
        not symbol
        or not expiry
        or not isinstance(current_chain, dict)
        or not isinstance(current_chain.get("data"), list)
        or not current_chain["data"]
    ):
        return {
            "available": False,
            "status": "unavailable",
            "reason": "symbol, expiry or current option chain unavailable",
            "data_source": "option_quote_history",
        }

    chain_symbol = str(current_chain.get("symbol") or symbol).upper().strip()
    chain_expiry = canonical_expiry(current_chain.get("expiry") or expiry)
    if chain_symbol != symbol or chain_expiry != expiry:
        return {
            "available": False,
            "status": "unavailable",
            "reason": "current chain symbol/expiry does not match requested contract set",
            "data_source": "option_quote_history",
        }

    def positive(value):
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError):
            return None

    now_utc = now_utc_naive()
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)

    ist_tz = ZoneInfo("Asia/Kolkata")
    now_ist = now_utc.replace(tzinfo=timezone.utc).astimezone(ist_tz)
    session_start_ist = datetime.combine(
        now_ist.date(),
        dt_time(hour=9, minute=15),
        tzinfo=ist_tz,
    )
    session_start_utc = (
        session_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    )
    cutoff = now_utc - timedelta(minutes=max(1, int(minutes_ago)))

    if cutoff < session_start_utc:
        return {
            "available": False,
            "status": "unavailable",
            "reason": "requested comparison window predates today's session start",
            "data_source": "option_quote_history",
        }

    stmt = (
        select(OptionQuoteHistory)
        .where(
            OptionQuoteHistory.symbol == symbol,
            expiry_matches(OptionQuoteHistory.expiry, expiry),
            OptionQuoteHistory.recorded_at >= session_start_utc,
            OptionQuoteHistory.recorded_at <= cutoff,
        )
        .order_by(OptionQuoteHistory.recorded_at.desc())
        .limit(5000)
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        return {
            "available": False,
            "status": "unavailable",
            "reason": f"no same-session quote snapshot older than {minutes_ago}m",
            "data_source": "option_quote_history",
        }

    reference_ts = rows[0].recorded_at
    if reference_ts.tzinfo is not None:
        reference_ts = reference_ts.astimezone(timezone.utc).replace(tzinfo=None)

    actual_minutes = round(
        (now_utc - reference_ts).total_seconds() / 60.0, 1
    )
    if actual_minutes > max_reference_age_minutes:
        return {
            "available": False,
            "status": "unavailable",
            "reason": (
                f"eligible reference snapshot is {actual_minutes:.1f}m old; "
                f"limit is {max_reference_age_minutes}m"
            ),
            "window_minutes": actual_minutes,
            "data_source": "option_quote_history",
        }

    # One saved quote batch shares a timestamp. Include only rows from that
    # same batch; nearby but different captures are not merged together.
    reference_rows = []
    for row in rows:
        row_ts = row.recorded_at
        if row_ts.tzinfo is not None:
            row_ts = row_ts.astimezone(timezone.utc).replace(tzinfo=None)
        if abs((reference_ts - row_ts).total_seconds()) <= 5:
            reference_rows.append(row)

    previous_by_contract = {}
    for row in reference_rows:
        old_price = positive(row.last_price)
        if old_price is None:
            continue
        previous_by_contract[(float(row.strike), str(row.option_type))] = {
            "price": old_price,
            "source": str(row.quote_source or ""),
        }

    raw_source = str(current_chain.get("data_source") or "").strip()
    source_lower = raw_source.lower()
    if source_lower == "angel_one":
        rest_source = "angel_one_rest"
    elif source_lower.startswith("zerodha"):
        rest_source = "zerodha_rest"
    else:
        rest_source = raw_source or "unknown"

    now_epoch = _time.time()
    current_by_contract = {}
    for item in current_chain["data"]:
        if not isinstance(item, dict):
            continue
        try:
            strike = float(item.get("strikePrice"))
            if not math.isfinite(strike) or strike <= 0:
                continue
        except (TypeError, ValueError):
            continue

        for option_type in ("CE", "PE"):
            leg = item.get(option_type)
            if not isinstance(leg, dict):
                continue

            ws = leg.get("live_ws_quote")
            if not isinstance(ws, dict):
                ws = {}

            ws_price = positive(ws.get("ltp"))
            ws_bid = positive(ws.get("bid"))
            ws_ask = positive(ws.get("ask"))
            try:
                received_at = float(ws.get("received_at") or 0)
                if ws.get("age_sec") is not None:
                    ws_age = float(ws["age_sec"])
                elif received_at > 0:
                    ws_age = now_epoch - received_at
                else:
                    ws_age = float("inf")
            except (TypeError, ValueError):
                received_at = 0
                ws_age = float("inf")

            ws_valid = (
                ws.get("source") == "angel_one_websocket"
                and received_at > 0
                and 0 <= ws_age <= 10
                and ws_price is not None
                and ws_bid is not None
                and ws_ask is not None
                and ws_ask >= ws_bid
            )

            if ws_valid:
                current_price = ws_price
                current_source = "angel_one_websocket"
                current_quote_timestamp = ws.get("exchange_timestamp")
            else:
                current_price = positive(leg.get("lastPrice"))
                current_source = rest_source
                current_quote_timestamp = (
                    leg.get("exchangeTradeTime")
                    or leg.get("exchangeFeedTime")
                )

            if current_price is None:
                continue

            current_by_contract[(strike, option_type)] = {
                "price": current_price,
                "source": current_source,
                "timestamp": current_quote_timestamp,
            }

    price_changes = []
    source_mismatch_count = 0
    for contract, current in current_by_contract.items():
        previous = previous_by_contract.get(contract)
        if previous is None:
            continue

        if not current["source"] or current["source"] != previous["source"]:
            source_mismatch_count += 1
            continue

        old_price = previous["price"]
        new_price = current["price"]
        delta = new_price - old_price
        price_changes.append({
            "strike": contract[0],
            "option_type": contract[1],
            "previous_price": old_price,
            "current_price": new_price,
            "price_change": round(delta, 4),
            "pct_change": round(100.0 * delta / old_price, 4),
            "quote_source": current["source"],
            "reference_recorded_at": reference_ts.isoformat(),
            "current_quote_timestamp": current["timestamp"],
        })

    if not price_changes:
        return {
            "available": False,
            "status": "unavailable",
            "reason": "no same-source contracts overlap between current and reference quotes",
            "window_minutes": actual_minutes,
            "matched_contracts": 0,
            "source_mismatch_count": source_mismatch_count,
            "reference_recorded_at": reference_ts.isoformat(),
            "data_source": "option_quote_history",
            "price_changes": [],
        }

    return {
        "available": True,
        "status": "available",
        "window_minutes": actual_minutes,
        "reference_recorded_at": reference_ts.isoformat(),
        "matched_contracts": len(price_changes),
        "source_mismatch_count": source_mismatch_count,
        "data_source": "option_quote_history",
        "price_changes": price_changes,
    }


async def analysis_history_due(
    symbol: str,
    snapshot_timestamp: str = None,
    interval_minutes: int = 5,
) -> bool:
    """Check whether a new AI history row is due before building it."""
    try:
        async with AsyncSessionLocal() as session:
            latest_row = (
                await session.execute(
                    select(AnalysisResult)
                    .where(AnalysisResult.symbol == symbol)
                    .where(AnalysisResult.analysis_type == "ai")
                    .order_by(AnalysisResult.timestamp.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if latest_row is None:
                return True

            latest_result = latest_row.result
            if not isinstance(latest_result, dict):
                latest_result = {}

            if (
                snapshot_timestamp
                and latest_result.get("market_snapshot_timestamp")
                == snapshot_timestamp
            ):
                return False

            if latest_row.timestamp:
                age = now_utc_naive() - latest_row.timestamp
                if age < timedelta(minutes=interval_minutes):
                    return False

            return True
    except Exception:
        logger.exception(
            "Analysis history due-check failed for %s; skipping extra work",
            symbol,
        )
        return False


async def save_analysis_result(
    symbol: str, analysis_type: str, result: Dict
) -> None:
    """Serialize history check-and-write so concurrent callers cannot race."""
    lock = _analysis_result_save_lock()
    async with lock:
        await _save_analysis_result_unlocked(symbol, analysis_type, result)


async def _save_analysis_result_unlocked(
    symbol: str, analysis_type: str, result: Dict
) -> None:
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


async def save_option_quote_snapshot(chain, fallback_symbol: str = "") -> bool:
    """Persist a separate, source-aware quote snapshot without changing OI history.

    The existing option_data table remains the OI-history source. This table
    preserves LTP/bid/ask, provider times, and WS timing for future replay.
    """
    import math
    import time as _time
    from app.models import OptionQuoteHistory
    from app.utils.expiry import canonical_expiry
    from app.utils.helpers import now_utc_naive

    if not isinstance(chain, dict) or not isinstance(chain.get("data"), list):
        return False

    symbol = str(chain.get("symbol") or fallback_symbol or "").upper().strip()
    expiry = canonical_expiry(chain.get("expiry") or "")
    if not symbol or not expiry:
        return False

    raw_source = str(chain.get("data_source") or "").strip()
    source_lower = raw_source.lower()
    if source_lower == "angel_one":
        rest_source = "angel_one_rest"
    elif source_lower.startswith("zerodha"):
        rest_source = "zerodha_rest"
    else:
        rest_source = raw_source or "unknown"

    def positive(value):
        try:
            n = float(value)
            return n if math.isfinite(n) and n > 0 else None
        except (TypeError, ValueError):
            return None

    recorded_at = now_utc_naive()
    now_epoch = _time.time()
    rows = []

    for item in chain["data"]:
        if not isinstance(item, dict):
            continue
        try:
            strike = float(item.get("strikePrice"))
            if not math.isfinite(strike) or strike <= 0:
                continue
        except (TypeError, ValueError):
            continue

        for option_type in ("CE", "PE"):
            leg = item.get(option_type)
            if not isinstance(leg, dict) or not leg:
                continue

            ws = leg.get("live_ws_quote")
            if not isinstance(ws, dict):
                ws = {}

            ws_ltp = positive(ws.get("ltp"))
            ws_bid = positive(ws.get("bid"))
            ws_ask = positive(ws.get("ask"))
            try:
                ws_received = float(ws.get("received_at") or 0)
            except (TypeError, ValueError):
                ws_received = 0.0

            ws_age = now_epoch - ws_received if ws_received > 0 else float("inf")
            ws_valid = (
                ws.get("source") == "angel_one_websocket"
                and ws_received > 0
                and 0 <= ws_age <= 10.0
                and ws_ltp is not None
                and ws_bid is not None
                and ws_ask is not None
                and ws_ask >= ws_bid
            )

            if ws_valid:
                last_price, bid, ask = ws_ltp, ws_bid, ws_ask
                quote_source = "angel_one_websocket"
                ws_received_at = ws_received
                ws_exchange_timestamp = ws.get("exchange_timestamp")
                ws_exchange_timestamp_ms = ws.get("exchange_timestamp_ms")
            else:
                last_price = positive(leg.get("lastPrice"))
                bid = positive(leg.get("bidprice", leg.get("bid")))
                ask = positive(leg.get("askPrice", leg.get("ask")))
                quote_source = rest_source
                ws_received_at = None
                ws_exchange_timestamp = None
                ws_exchange_timestamp_ms = None

            # Avoid storing empty contract/side records.
            if last_price is None and bid is None and ask is None:
                continue

            rows.append(OptionQuoteHistory(
                recorded_at=recorded_at,
                symbol=symbol,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                last_price=last_price,
                bid=bid,
                ask=ask,
                quote_source=quote_source,
                exchange_feed_time=(
                    str(leg["exchangeFeedTime"])
                    if leg.get("exchangeFeedTime") is not None else None
                ),
                exchange_trade_time=(
                    str(leg["exchangeTradeTime"])
                    if leg.get("exchangeTradeTime") is not None else None
                ),
                ws_received_at=ws_received_at,
                ws_exchange_timestamp=(
                    str(ws_exchange_timestamp)
                    if ws_exchange_timestamp is not None else None
                ),
                ws_exchange_timestamp_ms=(
                    str(ws_exchange_timestamp_ms)
                    if ws_exchange_timestamp_ms is not None else None
                ),
            ))

    if not rows:
        return False

    try:
        async with AsyncSessionLocal() as session:
            session.add_all(rows)
            await session.commit()
        return True
    except Exception as exc:
        logger.warning(
            "Failed to save source-aware option quote snapshot for %s: %s",
            symbol, exc,
        )
        return False
