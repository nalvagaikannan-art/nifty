from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.ledger_performance import (
    _action,
    _trade_return,
    _pnl_per_lot,
    _episode_side,
    _independent_episode_rows,
    _independent_contract_episode_rows,
    _horizon_in_session,
)


def test_ledger_action_normalization():
    assert _action("BUY CE") == "CALL_BUY"
    assert _action("BUY PE") == "PUT_BUY"
    assert _action("SELL CE") == "CALL_SELL"
    assert _action("SELL PE") == "PUT_SELL"


def test_direction_return_is_action_adjusted():
    assert _trade_return("BUY CE", 1.5) == 1.5
    assert _trade_return("SELL PE", 1.5) == 1.5
    assert _trade_return("BUY PE", 1.5) == -1.5
    assert _trade_return("SELL CE", 1.5) == -1.5


def test_option_pnl_is_action_adjusted_per_lot():
    assert _pnl_per_lot(100, 110, 65, "BUY CE") == 650
    assert _pnl_per_lot(110, 100, 65, "SELL PE") == 650
    assert _pnl_per_lot(100, 90, 65, "BUY CE") == -650
    assert _pnl_per_lot(90, 100, 65, "SELL CE") == -650


def _row(action, lifecycle, minute, base=None):
    ts = base or datetime(2026, 10, 7, 9, 15)
    return SimpleNamespace(
        action=action,
        lifecycle=lifecycle,
        timestamp=ts + timedelta(minutes=minute),
        market_snapshot_timestamp=None,
    )


def test_episode_side_requires_action_lifecycle_alignment():
    assert _episode_side(_row("BUY CE", "HOLD_CALL", 0)) == "CALL"
    assert _episode_side(_row("SELL PE", "CONFIRMED_CALL", 0)) == "CALL"
    assert _episode_side(_row("BUY PE", "HOLD_PUT", 0)) == "PUT"
    assert _episode_side(_row("SELL CE", "CONFIRMED_PUT", 0)) == "PUT"

    assert _episode_side(_row("BUY CE", "HOLD_PUT", 0)) is None
    assert _episode_side(_row("SELL PE", "CONFIRMED_PUT", 0)) is None
    assert _episode_side(_row("BUY CE", "UNKNOWN", 0)) is None


def test_independent_episode_rows_keep_first_same_side_after_10m_gap():
    base = datetime(2026, 10, 7, 9, 15)

    rows = [
        _row("BUY CE", "HOLD_CALL", 0, base),
        _row("BUY CE", "HOLD_CALL", 5, base),
        _row("BUY CE", "HOLD_CALL", 10, base),
        _row("BUY CE", "HOLD_CALL", 11, base),
        _row("SELL PE", "HOLD_CALL", 12, base),
        _row("BUY CE", "HOLD_CALL", 23, base),
    ]

    selected = _independent_episode_rows(rows)

    assert [r.timestamp for r in selected] == [
        rows[0].timestamp,
        rows[5].timestamp,
    ]


def test_independent_episode_rows_ignore_non_actionable_rows():
    base = datetime(2026, 10, 7, 9, 15)

    rows = [
        _row("WAIT", "UNKNOWN", 0, base),
        _row("BUY CE", "HOLD_CALL", 1, base),
        _row("BUY CE", "HOLD_CALL", 4, base),
        _row("BUY PE", "HOLD_PUT", 20, base),
        _row("BUY PE", "HOLD_PUT", 25, base),
    ]

    selected = _independent_episode_rows(rows)

    assert selected == [rows[1], rows[3]]


def test_contract_episode_grouping_keeps_distinct_contracts():
    base = datetime(2026, 10, 7, 9, 15)

    def make(action, minute, strike, opt):
        row = _row(action, "HOLD_CALL", minute, base)
        row.expiry = "13OCT2026"
        row.strike = strike
        row.option_type = opt
        return row

    call = make("BUY CE", 0, 22600, "CE")
    put_sell = make("SELL PE", 2, 22500, "PE")
    repeated_call = make("BUY CE", 5, 22600, "CE")
    call_after_gap = make("BUY CE", 16, 22600, "CE")

    assert _independent_contract_episode_rows(
        [call, put_sell, repeated_call, call_after_gap]
    ) == [call, put_sell, call_after_gap]


def test_contract_episode_grouping_normalizes_expiry_format():
    base = datetime(2026, 10, 7, 9, 15)

    first = _row("BUY CE", "HOLD_CALL", 0, base)
    first.expiry = "13OCT2026"
    first.strike = 22600
    first.option_type = "CE"

    alternate = _row("BUY CE", "HOLD_CALL", 5, base)
    alternate.expiry = "13-Oct-2026"
    alternate.strike = 22600
    alternate.option_type = "CE"

    assert _independent_contract_episode_rows([first, alternate]) == [first]


def test_ledger_horizon_accepts_exact_nse_close():
    # Naive timestamps represent UTC: 14:30 IST + 60m = 15:30 IST.
    assert _horizon_in_session(datetime(2026, 10, 14, 9, 0), 60) is True


def test_ledger_horizon_rejects_target_after_nse_close():
    # 14:31 IST + 60m = 15:31 IST.
    assert _horizon_in_session(datetime(2026, 10, 14, 9, 1), 60) is False


def test_ledger_horizon_rejects_preopen_event():
    assert _horizon_in_session(datetime(2026, 10, 14, 3, 0), 60) is False


def test_fresh_entry_performance_separates_new_entry_from_hold_and_legacy(
    monkeypatch,
):
    import asyncio
    import app.services.ledger_performance as lp

    # A prior HOLD row for this contract must not suppress the newer explicit
    # fresh-entry row from its independent contract episode set.
    base = (
        lp.now_utc_naive()
        .replace(hour=4, minute=0, second=0, microsecond=0)
        - timedelta(days=2)
    )
    while base.weekday() >= 5:
        base -= timedelta(days=1)

    def make_row(
        row_id, minute, lifecycle, entry_price,
        fresh_status=None, legacy=False,
    ):
        from app.services.entry_risk_authority import build_entry_risk_authority

        snapshot = {"lot_size": 65}
        if fresh_status is not None:
            decision = {
                "metadata_version": 1,
                "fresh_entry_allowed": fresh_status,
                "lifecycle": lifecycle,
                "signal_action": "BUY CE",
            }
            if fresh_status is True and not legacy:
                stop = entry_price - 2.0
                risk_per_lot = (entry_price - stop) * 65
                authority = build_entry_risk_authority(
                    action="BUY CE",
                    risk_assessment={
                        "allowed": True,
                        "lots": 1,
                        "quantity": 65,
                        "max_loss": risk_per_lot,
                        "max_risk_amount": risk_per_lot,
                        "risk_per_lot": risk_per_lot,
                        "no_trade_reasons": [],
                        "reasons": ["Test approval"],
                        "warnings": [],
                    },
                    selected_contract={
                        "symbol": "NIFTY",
                        "expiry": "20OCT2026",
                        "strike": 23000,
                        "type": "CE",
                        "entry_price": entry_price,
                        "sl": stop,
                    },
                    trade_levels={
                        "option_entry": entry_price,
                        "option_sl": stop,
                        "option_rr_ratio": 2.0,
                    },
                    lot_size=65,
                )
                decision["metadata_version"] = 2
                decision["entry_risk_authority"] = authority
            snapshot["strategy_decision"] = decision
        return SimpleNamespace(
            id=row_id,
            symbol="NIFTY",
            timestamp=base + timedelta(minutes=minute),
            market_snapshot_timestamp=None,
            action="BUY CE",
            lifecycle=lifecycle,
            option_type="CE",
            strike=23000.0,
            expiry="20OCT2026",
            spot=100.0,
            entry_price=entry_price,
            option_ltp_snapshot=entry_price,
            entry_snapshot=snapshot,
        )

    rows = [
        make_row(1, 0, "HOLD_CALL", 10.0, False),
        make_row(2, 5, "CONFIRMED_CALL", 12.0, True),
        make_row(3, 8, "CONFIRMED_CALL", 11.0, False),
        make_row(4, 9, "HOLD_CALL", 11.0),  # Missing metadata.
        make_row(
            5, 10, "CONFIRMED_CALL", 11.0,
            fresh_status=True, legacy=True,
        ),
    ]

    market_rows = [
        SimpleNamespace(timestamp=base + timedelta(minutes=60), price=101.0),
        SimpleNamespace(timestamp=base + timedelta(minutes=65), price=102.0),
    ]
    option_rows = [
        SimpleNamespace(
            timestamp=base + timedelta(minutes=60), last_price=11.0,
            symbol="NIFTY", expiry="20OCT2026", strike=23000.0,
            option_type="CE",
        ),
        SimpleNamespace(
            timestamp=base + timedelta(minutes=65), last_price=13.2,
            symbol="NIFTY", expiry="20OCT2026", strike=23000.0,
            option_type="CE",
        ),
    ]

    class FakeResult:
        def __init__(self, items):
            self.items = items

        def scalars(self):
            return self

        def all(self):
            return self.items

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement):
            description = statement.column_descriptions[0]
            if description.get("name") == "expiry":
                return FakeResult(["20OCT2026"])
            entity = description["entity"]
            if entity is lp.DailySignalLedger:
                return FakeResult(rows)
            if entity is lp.MarketData:
                return FakeResult(market_rows)
            if entity is lp.OptionData:
                return FakeResult(option_rows)
            raise AssertionError(f"Unexpected query entity: {entity}")

    monkeypatch.setattr(lp, "AsyncSessionLocal", lambda: FakeSession())
    result = asyncio.run(
        lp.compute_ledger_performance(
            "NIFTY", days=15, horizon_minutes=60
        )
    )

    # Existing headline metrics remain the all-actionable-episode metrics.
    assert result["ledger_rows"] == 5  # Includes the legacy V1 source row.
    assert result["contract_episodes"] == 1

    fresh = result["fresh_entry_performance"]
    assert fresh["ledger_rows_with_decision_metadata"] == 4
    assert fresh["ledger_rows_fresh_entry_allowed"] == 1
    assert fresh["ledger_rows_fresh_entry_denied"] == 1
    assert fresh["ledger_rows_hold_existing_direction"] == 1
    assert fresh["ledger_rows_legacy_risk_unverified"] == 1
    assert fresh["ledger_rows_unclassified"] == 1
    assert fresh["contract_episodes"] == 1
    assert fresh["contract_episode_results"][0]["id"] == 2
    assert fresh["option_data_available"] == 1
    assert fresh["full_pnl_ready"] == 1
    assert fresh["option_premium"]["action_adjusted_return_pct"]["average_pct"] == 10.0
    assert fresh["pnl"]["gross_pnl_per_lot"]["sum"] == 78.0


def test_option_union_query_preserves_expiry_semantics():
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine, select, and_, or_
    from sqlalchemy.orm import Session

    from app.models import OptionData
    from app.services.ledger_performance import (
        _build_option_history_statement,
        _deduplicate_option_rows,
    )
    from app.utils.expiry import expiry_matches

    engine = create_engine("sqlite:///:memory:")
    OptionData.__table__.create(engine)

    cutoff = datetime(2026, 10, 1, 9, 15)

    def option(expiry, strike, option_type, minutes, symbol="NIFTY"):
        return OptionData(
            symbol=symbol,
            expiry=expiry,
            strike=float(strike),
            option_type=option_type,
            last_price=100.0 + minutes,
            change=0.0,
            volume=100,
            open_interest=1000,
            implied_volatility=20.0,
            timestamp=cutoff + timedelta(minutes=minutes),
        )

    expected_rows = [
        option("27OCT2026", 23000, "CE", 1),
        option("27-Oct-2026", 23000, "CE", 2),
        option(" 27-Oct-2026 ", 23000, "CE", 3),
        option("27OCT2026", 23050, "CE", 4),
        option("WEEKLY-2026", 23100, "CE", 7),
    ]
    excluded_rows = [
        option("27NOV2026", 23000, "CE", 5),
        option("27OCT2026", 23000, "PE", 6),
        option("27OCT2026", 23000, "CE", -1),
        option("27OCT2026", 23000, "CE", 8, symbol="BANKNIFTY"),
    ]

    try:
        with Session(engine) as session:
            session.add_all(expected_rows + excluded_rows)
            session.flush()

            stored_expiries = session.scalars(
                select(OptionData.expiry)
                .where(OptionData.symbol == "NIFTY")
                .where(OptionData.timestamp >= cutoff)
                .where(OptionData.expiry.is_not(None))
                .distinct()
            ).all()

            contracts = {
                ("27OCT2026", 23000.0, "CE"),
                ("27OCT2026", 23050.0, "CE"),
                # Both legacy labels match the same raw row according to the
                # old expiry matcher; UNION ALL must not double-count it.
                ("WEEKLY-2026", 23100.0, "CE"),
                ("WEEKLY2026", 23100.0, "CE"),
            }

            old_filters = [
                and_(
                    expiry_matches(OptionData.expiry, expiry),
                    OptionData.strike == strike,
                    OptionData.option_type == opt,
                )
                for expiry, strike, opt in contracts
            ]
            baseline = session.scalars(
                select(OptionData)
                .where(OptionData.symbol == "NIFTY")
                .where(OptionData.timestamp >= cutoff)
                .where(or_(*old_filters))
            ).all()

            statement = _build_option_history_statement(
                "NIFTY", cutoff, contracts, stored_expiries
            )
            assert statement is not None
            candidate_raw = session.execute(statement).scalars().all()
            candidate = _deduplicate_option_rows(candidate_raw)

            assert {r.id for r in candidate} == {r.id for r in baseline}
            assert len(candidate) == len(baseline)
            assert len(candidate_raw) > len(candidate)

            # Valid date spellings, whitespace, strike/type filters, symbol
            # isolation, and cutoff behavior are included in this parity case.
            expected_ids = {r.id for r in expected_rows}
            assert expected_ids == {r.id for r in baseline}

    finally:
        engine.dispose()
