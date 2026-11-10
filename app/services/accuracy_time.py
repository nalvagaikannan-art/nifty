"""Shared event-time helpers for all accuracy calculations."""

from datetime import datetime, timezone
import logging

from app.models import AnalysisResult

logger = logging.getLogger(__name__)


def analysis_event_timestamp(row: AnalysisResult):
    """Return the canonical event time for an analysis row.

    New rows store the feature/snapshot-ready time in
    ``result['market_snapshot_timestamp']``.
    ``AnalysisResult.timestamp`` is only the database persistence time.

    Returns a naive UTC datetime for compatibility with existing DB timestamps.
    Older rows fall back to ``AnalysisResult.timestamp``.
    """
    result = row.result or {}
    raw = result.get("market_snapshot_timestamp")

    if raw:
        try:
            if isinstance(raw, datetime):
                ts = raw
            else:
                ts = datetime.fromisoformat(
                    str(raw).strip().replace("Z", "+00:00")
                )

            if ts.tzinfo is not None:
                ts = ts.astimezone(timezone.utc).replace(tzinfo=None)

            return ts

        except (TypeError, ValueError, OverflowError):
            logger.warning(
                "Invalid market_snapshot_timestamp for AnalysisResult id=%s; "
                "falling back to DB timestamp",
                getattr(row, "id", None),
            )

    ts = row.timestamp

    if ts is None:
        return None

    try:
        if isinstance(ts, str):
            ts = datetime.fromisoformat(
                ts.strip().replace("Z", "+00:00")
            )

        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)

        return ts

    except (TypeError, ValueError, OverflowError):
        logger.warning(
            "Invalid AnalysisResult timestamp for id=%s",
            getattr(row, "id", None),
        )
        return None
