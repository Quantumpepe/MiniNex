"""Native EVM SELL wraps exactly once under signed owner policy."""
from decimal import Decimal
import pytest
from fastapi import HTTPException

from backend import shared_evm_native_sell_deposit as dep
from backend.shared_evm_strategy_execution import derive_evm_strategy_signal_key
from backend.shared_evm_swap_receipts import _pair
from backend.storage import SCHEMA

DID="did:privy:owner"
RUN="owner_20261117T161000000000"
EVENT="market_166"
WALLET="0x"+"ab"*20
DIGEST="f"*64
ASSETS={"BNB":"USDT","POL":"USDC","AVAX":"USDC"}


def key(asset):
    return derive_evm_strategy_signal_key(
        did=DID,asset=asset,side="SELL",stable=ASSETS[asset],
        strategy_run_id=RUN,signal_event_id=EVENT)


def record(asset):
    return dict(wallet_address=WALLET,wallet_id="wallet_12345678",
                stable=ASSETS[asset],asset=asset,
                chain_id=_pair(asset,ASSETS[asset],"SELL")["chain_id"],
                policy_sha256="a"*64,budget_revision=5)


class Cursor:
    def __init__(self,rows):self.rows=iter(rows);self.sql=[]
    def execute(self,q,args):self.sql.append((q,args))
    def fetchone(self):return next(self.rows)


def rows(asset):
    p=record(asset)
    return [
        {"status":"ACTIVE","stable":p["stable"],"chain_id":p["chain_id"],
         "wallet_address":WALLET,"budget_revision":5},
        {"privy_did":DID},
        {"pending":0},{"pending":0},{"pending":0},{"pending":0},
    ]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_native_sell_claim_serializes_owner_and_three_network_journals(
    monkeypatch,asset
):
    p=record(asset)
    monkeypatch.setattr(dep,"_read_grant_evidence_locked",
                        lambda cur,**kw:p)
    monkeypatch.setattr(dep,"validate_pinned_evm_policy",
                        lambda *a,**kw:{"rules":[{}, {}, {"conditions":[]}, {}]})
    monkeypatch.setattr(dep,"_lte",lambda *a:str(10**20))
    cur=Cursor(rows(asset))
    dep._claim_native_sell_locked(
        cur,did=DID,asset=asset,key=key(asset),record=p,
        run_id=RUN,event_id=EVENT,amount_wei=10**18,
        digest=DIGEST,budget_revision=5)
    assert len(cur.sql)==7
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "mininex_accounts" in cur.sql[1][0]
    assert all("COUNT(*) AS pending" in q for q,a in cur.sql[2:5])
    assert "INSERT INTO mininex_live_evm_native_sell_intents" in cur.sql[-1][0]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("wrong,sql_count",[
    (lambda r:r[0].update(status="PAUSED"),1),
    (lambda r:r[2].update(pending=1),3),
    (lambda r:r[3].update(pending=1),4),
    (lambda r:r[4].update(pending=1),5),
])
def test_native_sell_claim_rejects_paused_or_unresolved_nonce(
    monkeypatch,asset,wrong,sql_count
):
    p=record(asset)
    monkeypatch.setattr(dep,"_read_grant_evidence_locked",
                        lambda cur,**kw:p)
    monkeypatch.setattr(dep,"validate_pinned_evm_policy",
                        lambda *a,**kw:{"rules":[{}, {}, {"conditions":[]}, {}]})
    monkeypatch.setattr(dep,"_lte",lambda *a:str(10**20))
    data=rows(asset)
    wrong(data)
    cur=Cursor(data)
    with pytest.raises(HTTPException):
        dep._claim_native_sell_locked(
            cur,did=DID,asset=asset,key=key(asset),record=p,
            run_id=RUN,event_id=EVENT,amount_wei=10**18,
            digest=DIGEST,budget_revision=5)
    assert len(cur.sql)==sql_count
    assert all("INSERT INTO mininex_live_evm_native_sell_intents" not in q
               for q,args in cur.sql)


@pytest.mark.parametrize("balance,gas,cap,expected",[
    ("1000000000000000000","4000000000000000","999999999999999999999",
     996000000000000000),
    ("1000000000000000000","4000000000000000","500000000000000000",
     500000000000000000),
])
def test_native_sell_uses_only_user_inventory_minus_network_cost(
    balance,gas,cap,expected
):
    assert dep.exact_native_sell_wei(native_atomic=balance,
            gas_atomic=gas,deposit_cap_atomic=cap)==expected


@pytest.mark.parametrize("balance,gas,cap",[
    ("100","100","100"),("10","20","100"),("NaN","1","100"),
    ("100","0","100"),("-1","1","100"),("100","1","0"),
])
def test_no_real_native_send_without_exact_amount_and_gas(balance,gas,cap):
    with pytest.raises(ValueError):
        dep.exact_native_sell_wei(
            native_atomic=balance,gas_atomic=gas,
            deposit_cap_atomic=cap)


def test_native_sell_cannot_change_original_execution_id():
    original=dep.native_sell_deposit_key(DID,"BNB",key("BNB"))
    assert original.startswith("native_")
    assert original!=key("BNB")
    assert original!=dep.native_sell_deposit_key(DID,"POL",key("POL"))
    assert original==dep.native_sell_deposit_key(DID,"BNB",key("BNB"))


def test_ambiguous_native_sell_never_retries():
    cur=Cursor([{"status":"RECONCILIATION_REQUIRED"}])
    result=dep._save_native_sell_result_locked(
        cur,did=DID,asset="BNB",key=key("BNB"),
        digest=DIGEST,tx_hash=None)
    assert result=="RECONCILIATION_REQUIRED"
    assert "AND status='SENDING'" in cur.sql[0][0]


def test_original_sell_deposit_table_and_no_user_send_route():
    ddl="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_live_evm_native_sell_intents" in ddl
    assert "UNIQUE(chain_id,tx_hash)" in ddl
    from backend.app import app
    assert all("native-sell-deposit" not in route.path for route in app.routes)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_an_unfinished_previous_native_sell_blocks_another_wrap(monkeypatch,asset):
    p=record(asset)
    monkeypatch.setattr(dep,"_read_grant_evidence_locked",
                        lambda cur,**kw:p)
    monkeypatch.setattr(dep,"validate_pinned_evm_policy",
                        lambda *args,**kwargs:{"rules":[{}, {}, {"conditions":[]}, {}]})
    monkeypatch.setattr(dep,"_lte",lambda *args:str(10**20))
    r=rows(asset)
    r[-1]={"pending":1}
    cur=Cursor(r)
    with pytest.raises(HTTPException) as err:
        dep._claim_native_sell_locked(
            cur,did=DID,asset=asset,key=key(asset),record=p,
            run_id=RUN,event_id=EVENT,amount_wei=10**18,
            digest=DIGEST,budget_revision=5)
    assert err.value.status_code==409
    assert "LEFT JOIN mininex_live_evm_auto_broadcasts" in cur.sql[-1][0]
    assert not any("INSERT INTO mininex_live_evm_native_sell_intents" in q
                   for q,args in cur.sql)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_native_fee_reserve_uses_live_network_gas_quotes(monkeypatch,asset):
    import asyncio
    import httpx
    pair=_pair(asset,ASSETS[asset],"SELL")
    async def rpc(client,url,method,params,rid):
        assert url.startswith("https://")
        if method=="eth_chainId":
            return hex(pair["chain_id"])
        if method=="eth_gasPrice":
            return hex(3000000000)
        assert method=="eth_estimateGas"
        assert params[0]["to"].lower()==pair["source_token"].lower()
        return hex(55000)
    monkeypatch.setattr(dep,"_rpc",rpc)
    source={"wallet_identity_verified":True,"asset":asset,
            "network":pair["network"],"chain_id":pair["chain_id"],
            "wallet_address":WALLET}
    async def measure():
        async with httpx.AsyncClient() as client:
            return await dep.estimate_native_sell_fee_reserve(
                asset=asset,stable=ASSETS[asset],
                source_evidence=source,client=client)
    assert asyncio.run(measure())==3000000000*(55000*2+350000)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_native_fee_quote_rejects_rpc_wrong_chain(monkeypatch,asset):
    import asyncio
    import httpx
    pair=_pair(asset,ASSETS[asset],"SELL")
    async def rpc(client,url,method,params,rid):
        return "0x1"
    monkeypatch.setattr(dep,"_rpc",rpc)
    source={"wallet_identity_verified":True,"asset":asset,
            "network":pair["network"],"chain_id":pair["chain_id"],
            "wallet_address":WALLET}
    async def measure():
        async with httpx.AsyncClient() as client:
            return await dep.estimate_native_sell_fee_reserve(
                asset=asset,stable=ASSETS[asset],
                source_evidence=source,client=client)
    with pytest.raises(HTTPException) as err:
        asyncio.run(measure())
    assert err.value.status_code==502


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_prepared_native_deposit_uses_user_policy_cap_not_system_amount(
    monkeypatch,asset
):
    import asyncio
    import httpx
    pair=_pair(asset,ASSETS[asset],"SELL")
    p=record(asset)
    monkeypatch.setattr(dep,"_read_grant_record",lambda did,asset:p)
    monkeypatch.setattr(dep,"validate_pinned_evm_policy",
                        lambda *a,**kw:{"rules":[{}, {}, {"conditions":[]}, {}]})
    monkeypatch.setattr(dep,"_lte",lambda *a:str(2*10**18))
    async def gas(**kw):
        return 10**15
    monkeypatch.setattr(dep,"estimate_native_sell_fee_reserve",gas)
    source={"mode":"SHARED_EVM_SOURCE_ALLOWANCE_READ_ONLY",
            "asset":asset,"side":"SELL","stable":ASSETS[asset],
            "chain_id":pair["chain_id"],
            "source_token_address":pair["source_token"],
            "wallet_identity_verified":True,"wallet_address":WALLET,
            "native_gas_balance_atomic":str(3*10**18)}
    async def prepare():
        async with httpx.AsyncClient() as client:
            return await dep.prepare_original_native_sell_amount(
                did=DID,asset=asset,stable=ASSETS[asset],
                source_evidence=source,client=client)
    assert asyncio.run(prepare())==2*10**18
