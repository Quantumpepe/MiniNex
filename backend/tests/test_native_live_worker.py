"""No native automatic worker starts without an actual owner grant verifier."""
import asyncio
from datetime import datetime,timezone

import pytest
from backend import native_live_worker as m


def test_native_auto_needs_no_extra_technical_live_switches(monkeypatch):
    for name in ("MININEX_NATIVE_AUTO_WORKER_ENABLED",
                 "MININEX_LIVE_TRADING_ENABLED","DATABASE_URL"):
        monkeypatch.delenv(name,raising=False)
    assert m.native_auto_worker_enabled() is False
    monkeypatch.setenv("DATABASE_URL","postgres://fake")
    assert m.native_auto_worker_enabled() is True
    # No hidden operator switches needed after the user's owner grant.
    monkeypatch.setenv("MININEX_NATIVE_AUTO_WORKER_ENABLED","false")
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    assert m.native_auto_worker_enabled() is True
    async def test():
        worker=m.NativeLiveWorker(object(),object())
        await worker.start()
        assert worker._task is None
        await worker.stop()
    asyncio.run(test())


def test_owner_started_native_sessions_queries_only_active_current_budgets(monkeypatch):
    monkeypatch.delenv("MININEX_NATIVE_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    monkeypatch.setenv("DATABASE_URL","postgres://unused")
    class Cur:
        def __enter__(self):return self
        def __exit__(self,*x):return False
        def execute(self,sql,args):
            assert "s.status='ACTIVE'" in sql
            assert "b.percent>0" in sql
            assert "e.budget_usd>0" in sql
            assert "s.asset='BTC'" in sql
            assert "s.asset!='BTC'" in sql
            assert "s.started_by_owner_at IS NOT NULL" in sql
        def fetchall(self):return []
    class DB:
        def __enter__(self):return self
        def __exit__(self,*x):return False
        def cursor(self,**kw):return Cur()
    monkeypatch.setattr(m,"_db_url",lambda:"postgres://unused")
    monkeypatch.setattr(m,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(m.psycopg,"connect",lambda *a,**kw:DB())
    monkeypatch.setattr(m,"_PAGE_OFFSET",0)
    assert m.owner_started_native_sessions()==[]
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    # Changing an obsolete operator switch cannot block owner sessions.
    assert m.owner_started_native_sessions()==[]


@pytest.mark.parametrize("asset",["BTC","SOL","XRP","TRX","DOGE"])
def test_market_adapter_uses_the_actual_coin_asset_feed(asset):
    from backend.native_live_auto_tick import OriginalNativeMarket
    class Fake:
        def __init__(self,k):self.k=k
        def snapshot(self,*,now):
            return ({"markets":{self.k:{"price_usdt":1}}} if self.k=="BTC"
                    else {asset:{"price_usdt":2}})
        def history(self,asset,*,limit):
            return [{"timestamp":1,"price_usdt":3}]
    marker=asset if asset!="BTC" else "SOL"
    v=OriginalNativeMarket(Fake("BTC"),Fake(marker))
    if asset!="BTC":
        assert asset in v.snapshot(now=1800000000)["markets"]
    else:
        assert "BTC" in v.snapshot(now=1800000000)["markets"]
    assert v.history(asset,limit=5)[0]["price_usdt"]==3
