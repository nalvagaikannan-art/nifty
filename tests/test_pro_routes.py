import random
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api import deps
from app.api.routes import pro
from app.config import settings
from app.database import Base, engine, AsyncSessionLocal
from app.services import order_executor as ox
from app.services.iv_history import IvHistory
from app.utils.helpers import now_ist
from tests.pro_helpers import make_chain


class FakeFetcher:
    async def get_option_chain(self, symbol, expiry=None, strikes_each_side=10):
        chain = make_chain(iv=14.0)
        chain["symbol"] = symbol
        chain.pop("lot_size", None)  # exercise the symbol-specific fallback
        return chain

    async def get_volatility_info(self):
        return {"value": 15.0, "available": True}

    async def get_spot(self, symbol):
        return {"change_percent": -0.2}


@pytest.fixture
async def client(monkeypatch, tmp_path):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as s:
        await s.execute(delete(ox.ProOrder)); await s.execute(delete(IvHistory))
        rnd = random.Random(1)
        today = now_ist().date()
        for i in range(1, 61):                       # 60 past days, IV 8..16 -> today's 14 ≈ rank 75
            s.add(IvHistory(symbol="NIFTY", trade_date=(today - timedelta(days=i)).isoformat(), atm_iv=8 + rnd.random() * 8))
        await s.commit()
    monkeypatch.setattr(settings, "kill_switch_file", str(tmp_path / "KILL"))
    for k, v in dict(trading_mode="paper", live_trading_enabled=False, order_pin="9999", max_lots_per_order=1,
                     max_orders_per_day=10, allow_naked_sell=False).items():
        monkeypatch.setattr(settings, k, v)
    ox._runtime_kill["on"] = False
    app = FastAPI()
    app.include_router(pro.router, prefix="/api/pro")
    app.dependency_overrides[deps.get_fetcher] = lambda: FakeFetcher()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    ox._runtime_kill["on"] = False


async def test_status_and_overview(client):
    assert (await client.get("/api/pro/status")).json()["mode"] == "paper"
    d = (await client.get("/api/pro/overview/NIFTY?adx=15")).json()
    assert d["iv"]["reliable"] and d["signals"], d["wait_reasons"]
    assert d["lot_size"] == 65 and d["metrics"]["ok"]



@pytest.mark.parametrize(
    ("symbol", "expected_lot_size"),
    [("NIFTY", 65), ("BANKNIFTY", 30), ("FINNIFTY", 60), ("SENSEX", 20)],
)
async def test_pro_overview_supports_all_four_index_contract_sizes(
    client, symbol, expected_lot_size
):
    response = await client.get(f"/api/pro/overview/{symbol}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == symbol
    assert payload["lot_size"] == expected_lot_size


async def test_unknown_symbol_400(client):
    assert (await client.get("/api/pro/overview/BANKEX")).status_code == 400


async def test_preview_and_paper_execute_use_server_prices(client):
    d = (await client.get("/api/pro/overview/NIFTY?adx=15")).json()
    ic = next(s for s in d["signals"] if s["type"] == "IRON_CONDOR")
    body = {"symbol": "NIFTY", "signal_id": ic["id"], "lots": 1}
    p = await client.post("/api/pro/orders/preview?adx=15", json=body)
    # preview rebuilds WITHOUT adx -> proxy range-bound (synthetic chain passes) -> same id
    assert p.status_code == 200 and len(p.json()["orders"]) == 4
    e = await client.post("/api/pro/orders/execute", json=body)
    assert e.status_code == 200 and e.json()["mode"] == "paper" and e.json()["complete"]
    assert len((await client.get("/api/pro/orders")).json()["orders"]) == 4


async def test_unknown_signal_is_409_and_lots_limit_422(client):
    r = await client.post("/api/pro/orders/execute", json={"symbol": "NIFTY", "signal_id": "nope", "lots": 1})
    assert r.status_code == 409
    d = (await client.get("/api/pro/overview/NIFTY")).json()
    sid = d["signals"][0]["id"]
    r = await client.post("/api/pro/orders/execute", json={"symbol": "NIFTY", "signal_id": sid, "lots": 5})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LOT_LIMIT"


async def test_kill_switch_pin_rules(client):
    assert (await client.post("/api/pro/kill-switch", json={"on": True})).json()["kill_switch"] is True
    assert (await client.post("/api/pro/kill-switch", json={"on": False})).status_code == 403
    assert (await client.post("/api/pro/kill-switch", json={"on": False}, headers={"X-Order-Pin": "bad"})).status_code == 403
    assert (await client.post("/api/pro/kill-switch", json={"on": False}, headers={"X-Order-Pin": "9999"})).json()["kill_switch"] is False


async def test_news_and_commodities_degrade_gracefully(client, monkeypatch):
    monkeypatch.setattr(settings, "newsapi_key", None)
    assert (await client.get("/api/pro/news/markets")).json()["source"] == "unavailable"
    from app.services import commodity_feed

    async def snap():
        return {"_kite_live": False, "CRUDEOIL": {"change_pct": 2.0, "rsi15": None}, "GOLD": {}, "SILVER": {}, "USDINR": {"change_pct": 0.1}}
    monkeypatch.setattr(commodity_feed, "get_commodity_snapshot", snap)
    d = (await client.get("/api/pro/commodities")).json()
    assert d["impact"]["nifty_impact_pct"] == -0.8 and d["ideas"][0]["symbol"] == "CRUDEOIL"
