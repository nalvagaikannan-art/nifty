from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import history_service


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, statement):
        compiled = statement.compile()
        self.sql = str(compiled)
        self.params = compiled.params

        datetimes = [
            value
            for value in self.params.values()
            if isinstance(value, datetime)
        ]
        assert len(datetimes) >= 2
        start_utc = min(datetimes)
        cutoff_utc = max(datetimes)

        # Simulate the database applying its timestamp predicates.
        eligible = [
            row for row in self.rows
            if start_utc <= row.timestamp <= cutoff_utc
        ]
        return FakeResult(eligible)


def current_chain():
    return pd.DataFrame([{
        "strike": 25000.0,
        "ce_oi": 110,
        "ce_ltp": 105.0,
        "pe_oi": 90,
        "pe_ltp": 90.0,
    }])


def saved_row(timestamp, option_type, open_interest, last_price):
    return SimpleNamespace(
        timestamp=timestamp,
        strike=25000.0,
        option_type=option_type,
        open_interest=open_interest,
        last_price=last_price,
    )


@pytest.mark.asyncio
async def test_oi_history_excludes_previous_trading_session(monkeypatch):
    # Monday 10:30 IST; Friday's snapshot must not be selected.
    now = datetime(2026, 10, 12, 5, 0)
    friday_snapshot = datetime(2026, 10, 9, 9, 52)

    session = FakeSession([
        saved_row(friday_snapshot, "CE", 100, 100.0),
        saved_row(friday_snapshot, "PE", 100, 95.0),
    ])
    monkeypatch.setattr(
        history_service, "now_utc_naive", lambda: now
    )
    monkeypatch.setattr(
        history_service, "AsyncSessionLocal", lambda: session
    )

    result = await history_service.get_oi_change_since(
        "FINNIFTY", "27OCT2026", current_chain(), minutes_ago=15
    )

    expected_session_start = datetime(2026, 10, 12, 3, 45)
    expected_cutoff = datetime(2026, 10, 12, 4, 45)

    assert "option_data.timestamp >=" in session.sql
    assert "option_data.timestamp <=" in session.sql
    assert expected_session_start in session.params.values()
    assert expected_cutoff in session.params.values()
    assert result["available"] is False
    assert "history still building" in result["reason"]


@pytest.mark.asyncio
async def test_oi_history_accepts_eligible_same_session_snapshot(monkeypatch):
    # A same-session snapshot older than the requested 15-minute cutoff.
    now = datetime(2026, 10, 12, 5, 0)
    reference = datetime(2026, 10, 12, 4, 30)

    session = FakeSession([
        saved_row(reference, "CE", 100, 100.0),
        saved_row(reference, "PE", 100, 95.0),
    ])
    monkeypatch.setattr(
        history_service, "now_utc_naive", lambda: now
    )
    monkeypatch.setattr(
        history_service, "AsyncSessionLocal", lambda: session
    )

    result = await history_service.get_oi_change_since(
        "FINNIFTY", "27-Oct-2026", current_chain(), minutes_ago=15
    )

    assert result["available"] is True
    assert result["window_minutes"] == 30.0
    assert result["ce_oi_change"] == 10
    assert result["pe_oi_change"] == -10

@pytest.mark.asyncio
async def test_closed_market_expiry_discovery_selects_nearest_complete_future_snapshot(
    monkeypatch,
):
    class ExpiryListResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class ExpiryListSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement):
            return ExpiryListResult([
                "19OCT2026", "27-Oct-2026", "27OCT2026",
                "17NOV2026", "30MAR2027", "28SEP2027",
                "26DEC2028", "not-a-date",
            ])

    cutoff = datetime(2026, 10, 21, 17, 19)
    monkeypatch.setattr(
        history_service, "AsyncSessionLocal", ExpiryListSession
    )

    def snapshot(expiry):
        return {
            "symbol": "NIFTY",
            "expiry": expiry,
            "all_expiries": [expiry],
            "underlying_price": 25000.0,
            "data": [{
                "strikePrice": 25000.0,
                "CE": {"lastPrice": 100.0},
                "PE": {"lastPrice": 95.0},
            }],
            "data_source": "history_db_closed_fallback",
            "market_open": False,
        }

    snapshots = {
        "27OCT2026": snapshot("27OCT2026"),
        "17NOV2026": None,
        "30MAR2027": snapshot("30MAR2027"),
        "28SEP2027": snapshot("28SEP2027"),
        "26DEC2028": snapshot("26DEC2028"),
    }
    looked_up = []

    async def lookup(symbol, expiry, lookup_cutoff):
        assert symbol == "NIFTY"
        assert lookup_cutoff == cutoff
        looked_up.append(expiry)
        return snapshots.get(expiry)

    monkeypatch.setattr(
        history_service, "get_latest_option_chain_snapshot_before", lookup
    )

    result = (
        await history_service
        .get_latest_option_chain_snapshot_for_nearest_expiry_before(
            "nifty", cutoff
        )
    )

    assert looked_up == [
        "27OCT2026", "17NOV2026", "30MAR2027",
        "28SEP2027", "26DEC2028",
    ]
    assert result["expiry"] == "27OCT2026"
    assert result["all_expiries"] == [
        "27OCT2026", "30MAR2027", "28SEP2027", "26DEC2028",
    ]
    assert result["data"]
    assert result["market_open"] is False


@pytest.mark.asyncio
async def test_closed_market_expiry_discovery_returns_none_without_future_chain(
    monkeypatch,
):
    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return ["19OCT2026", "not-a-date"]

    class EmptySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement):
            return EmptyResult()

    monkeypatch.setattr(
        history_service, "AsyncSessionLocal", EmptySession
    )

    result = (
        await history_service
        .get_latest_option_chain_snapshot_for_nearest_expiry_before(
            "NIFTY", datetime(2026, 10, 21, 17, 19)
        )
    )
    assert result is None

@pytest.mark.asyncio
async def test_oi_history_returns_complete_atm_evidence_from_same_reference(monkeypatch):
    now = datetime(2026, 10, 22, 5, 0)
    reference = datetime(2026, 10, 22, 4, 30)

    saved = []
    for strike, side, oi, price in [
        (24950.0, "CE", 100, 100.0),
        (24950.0, "PE", 200, 50.0),
        (25000.0, "CE", 100, 100.0),
        (25000.0, "PE", 100, 95.0),
        (25050.0, "CE", 100, 100.0),
        (25050.0, "PE", 100, 60.0),
    ]:
        saved.append(SimpleNamespace(
            timestamp=reference, strike=strike, option_type=side,
            open_interest=oi, last_price=price,
        ))

    current = pd.DataFrame([
        {"strike": 24950.0, "ce_oi": 110, "ce_ltp": 105.0,
         "pe_oi": 190, "pe_ltp": 51.0},
        {"strike": 25000.0, "ce_oi": 110, "ce_ltp": 95.0,
         "pe_oi": 105, "pe_ltp": 90.0},
        {"strike": 25050.0, "ce_oi": 100, "ce_ltp": 101.0,
         "pe_oi": 100, "pe_ltp": 61.0},
    ])

    session = FakeSession(saved)
    monkeypatch.setattr(history_service, "now_utc_naive", lambda: now)
    monkeypatch.setattr(history_service, "AsyncSessionLocal", lambda: session)

    result = await history_service.get_oi_change_since(
        "NIFTY", "27OCT2026", current,
        minutes_ago=15, underlying=25000.0,
    )

    assert result["available"] is True
    evidence = result["atm_evidence"]
    assert evidence["available"] is True
    assert evidence["atm_strikes"] == [24950.0, 25000.0, 25050.0]
    assert evidence["expected_contracts_per_side"] == 3
    assert evidence["ce_contracts"] == evidence["pe_contracts"] == 3
    assert evidence["ce_oi_change"] == 20
    assert evidence["pe_oi_change"] == -5
    assert evidence["ce_price_change"] == 0.33
    assert evidence["pe_price_change"] == -1.0
    assert evidence["window_minutes"] == 30.0
    assert evidence["data_source"] == "option_data_same_session_reference"



# OI_HISTORY_COVERAGE_REGRESSION_TESTS_20261023
@pytest.mark.asyncio
async def test_oi_history_marks_sparse_reference_coverage_unavailable(monkeypatch):
    # Only one of four current strikes has a reference row on each side.
    now = datetime(2026, 10, 12, 5, 0)
    reference = datetime(2026, 10, 12, 4, 30)
    saved = [
        SimpleNamespace(
            timestamp=reference, strike=100.0, option_type="CE",
            open_interest=100, last_price=10.0,
        ),
        SimpleNamespace(
            timestamp=reference, strike=100.0, option_type="PE",
            open_interest=100, last_price=10.0,
        ),
    ]
    current = pd.DataFrame([
        {"strike": 100.0, "ce_oi": 110, "pe_oi": 105,
         "ce_ltp": 11.0, "pe_ltp": 10.5},
        {"strike": 105.0, "ce_oi": 120, "pe_oi": 115,
         "ce_ltp": 9.0, "pe_ltp": 11.0},
        {"strike": 110.0, "ce_oi": 130, "pe_oi": 125,
         "ce_ltp": 8.0, "pe_ltp": 12.0},
        {"strike": 115.0, "ce_oi": 140, "pe_oi": 135,
         "ce_ltp": 7.0, "pe_ltp": 13.0},
    ])

    session = FakeSession(saved)
    monkeypatch.setattr(history_service, "now_utc_naive", lambda: now)
    monkeypatch.setattr(history_service, "AsyncSessionLocal", lambda: session)

    result = await history_service.get_oi_change_since(
        "NIFTY", "27OCT2026", current,
        minutes_ago=15, underlying=0.0,
    )

    assert result["available"] is False
    assert result["status"] == "unavailable"
    assert result["coverage"]["ce_coverage_pct"] == 25.0
    assert result["coverage"]["pe_coverage_pct"] == 25.0
    assert result["ce_oi_change"] is None
    assert result["pe_oi_change"] is None
    assert "70%" in result["reason"]
