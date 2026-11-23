import pytest
from sqlalchemy import delete

from app.config import settings
from app.database import Base, engine, AsyncSessionLocal
from app.services import order_executor as ox
from app.services.iv_history import IvHistory, record_iv, get_iv_values
from app.services.sell_signals import generate_sell_signals
from tests.pro_helpers import build_ctx


@pytest.fixture(autouse=True)
async def _db(monkeypatch, tmp_path):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as s:
        await s.execute(delete(ox.ProOrder))
        await s.execute(delete(IvHistory))
        await s.commit()
    monkeypatch.setattr(settings, "kill_switch_file", str(tmp_path / "KILL"))
    ox._runtime_kill["on"] = False
    monkeypatch.setattr(settings, "trading_mode", "paper")
    monkeypatch.setattr(settings, "live_trading_enabled", False)
    monkeypatch.setattr(settings, "order_pin", None)
    monkeypatch.setattr(settings, "max_lots_per_order", 1)
    monkeypatch.setattr(settings, "max_orders_per_day", 10)
    monkeypatch.setattr(settings, "allow_naked_sell", False)
    yield
    ox._runtime_kill["on"] = False


def sig(kind="IRON_CONDOR"):
    res = generate_sell_signals(build_ctx(iv_rank=85, adx=15))
    return next(s for s in res["signals"] if s["type"] == kind)


def test_build_orders_hedges_first():
    o = ox.build_orders(sig(), 1)
    assert [x["side"] for x in o] == ["BUY", "BUY", "SELL", "SELL"] and all(x["quantity"] == 65 for x in o)


def test_validate_limits():
    s = sig()
    with pytest.raises(ox.OrderRejected) as e:
        ox.validate(s, 2, True, 0)
    assert e.value.code == "LOT_LIMIT"
    with pytest.raises(ox.OrderRejected) as e:
        ox.validate(s, 1, True, 8)
    assert e.value.code == "DAILY_LIMIT"
    assert len(ox.validate(s, 1, True, 0)) == 4


def test_naked_sell_blocked_unless_allowed(monkeypatch):
    s = sig("SHORT_STRANGLE")
    with pytest.raises(ox.OrderRejected) as e:
        ox.validate(s, 1, False, 0)           # hedge wings removed -> naked
    assert e.value.code == "NAKED_SELL"
    assert len(ox.validate(s, 1, True, 0)) == 4   # with wings OK
    monkeypatch.setattr(settings, "allow_naked_sell", True)
    assert len(ox.validate(s, 1, False, 0)) == 2


def test_advisory_and_bad_price_rejected():
    s = sig(); s["executable"] = False
    with pytest.raises(ox.OrderRejected) as e:
        ox.validate(s, 1, True, 0)
    assert e.value.code == "ADVISORY_ONLY"
    s = sig(); s["legs"][0]["price"] = 0
    with pytest.raises(ox.OrderRejected) as e:
        ox.validate(s, 1, True, 0)
    assert e.value.code == "BAD_PRICE"


async def test_kill_switch_blocks():
    ox.set_kill_switch(True)
    assert ox.status()["kill_switch"] is True
    with pytest.raises(ox.OrderRejected) as e:
        await ox.execute_signal(sig(), 1)
    assert e.value.code == "KILL_SWITCH"
    ox.set_kill_switch(False)
    assert ox.status()["kill_switch"] is False


async def test_paper_execute_logs_and_counts_toward_daily_limit():
    r = await ox.execute_signal(sig(), 1)
    assert r["mode"] == "paper" and r["complete"] and len(r["legs"]) == 4
    assert [l["side"] for l in r["legs"]] == ["BUY", "BUY", "SELL", "SELL"]
    rows = await ox.list_orders()
    assert len(rows) == 4 and all(x["status"] == "PAPER_FILLED" for x in rows)
    assert await ox._orders_today() == 4


async def test_live_needs_pin(monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    with pytest.raises(ox.OrderRejected) as e:           # live but no ORDER_PIN configured
        await ox.execute_signal(sig(), 1, pin="x")
    assert e.value.code == "PIN_NOT_CONFIGURED"
    monkeypatch.setattr(settings, "order_pin", "4321")
    with pytest.raises(ox.OrderRejected) as e:
        await ox.execute_signal(sig(), 1, pin="bad")
    assert e.value.code == "BAD_PIN"
    assert await ox.list_orders() == []                  # nothing logged, nothing sent


async def test_live_flow_order_and_stop_on_failure(monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "order_pin", "4321")
    sent = []

    async def cash():
        return 1_000_000.0

    async def place(o, symbol, spot=0.0):
        if len(sent) == 2:
            raise RuntimeError("broker rejected (static IP)")
        sent.append((o["side"], o["strike"], o["option_type"]))
        return {"tradingsymbol": "X", "exchange": "NFO", "broker_order_id": f"B{len(sent)}"}
    monkeypatch.setattr(ox, "_free_cash", cash)
    monkeypatch.setattr(ox, "_angel_place", place)
    r = await ox.execute_signal(sig(), 1, pin="4321")
    assert r["mode"] == "live" and r["complete"] is False and "கைமுறையாக" in r["warning"]
    assert [s[0] for s in sent] == ["BUY", "BUY"]        # hedges went; SELL legs never sent
    statuses = [x["status"] for x in await ox.list_orders()]
    assert statuses.count("SENT") == 2 and statuses.count("ERROR") == 1


async def test_live_low_cash_rejected(monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", "live")
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "order_pin", "4321")

    async def cash():
        return 1000.0
    monkeypatch.setattr(ox, "_free_cash", cash)
    with pytest.raises(ox.OrderRejected) as e:
        await ox.execute_signal(sig(), 1, pin="4321")
    assert e.value.code == "LOW_CASH" and await ox.list_orders() == []


async def test_angel_place_rejects_wrong_expiry(monkeypatch):
    import app.services.angel_one as ao

    class Fake:
        DERIVATIVE_EXCHANGE_MAP = {"NIFTY": "NFO"}
        _obj = None

        async def ensure_session(self): pass

        async def resolve_option_tokens(self, *a, **k):   # silently returns NEAREST expiry (real behaviour)
            return [{"strike": 22850.0, "optionType": "CE", "expiry": "10NOV2026", "token": "1", "tradingsymbol": "NIFTY10NOV2622850CE"}]
    monkeypatch.setattr(ao, "angel_session", Fake())
    o = {"expiry": "17NOV2026", "strike": 22850.0, "option_type": "CE", "side": "SELL", "price": 10, "quantity": 65}
    with pytest.raises(ox.OrderRejected) as e:
        await ox._angel_place(o, "NIFTY")
    assert e.value.code == "EXPIRY_MISMATCH"


async def test_iv_history_roundtrip():
    assert await record_iv("NIFTY", 14.5, vix=15, spot=22500, force=True)
    assert await record_iv("NIFTY", 15.5, force=True)            # same day -> upsert
    assert await get_iv_values("NIFTY", exclude_today=False) == [15.5]
    assert await get_iv_values("NIFTY") == []                    # today excluded from the baseline
    assert not await record_iv("NIFTY", 0, force=True)
