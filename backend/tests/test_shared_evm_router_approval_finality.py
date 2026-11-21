"""One-shot ERC20 Router02 grant: only original chain-finalized approval is usable."""
import pytest

from backend.shared_evm_router_approval_finality import _verify_approval_finality
from backend.shared_evm_router_approval_send import _original_approval_id
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
HASH="0x"+"cd"*32
BLOCK="0x"+"ef"*32

@pytest.mark.parametrize("asset,stable",[
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
    ("LINK","USDC"),("UNI","USDT"),("ARB","USDC"),
])
def test_exact_original_allowance_finality_and_reorg_guards(asset,stable):
    pair=_pair(asset,stable,"BUY")
    row=dict(asset=asset,stable=stable,side="BUY",
        chain_id=pair["chain_id"],wallet_address=WALLET,
        token_address=pair["source_token"].lower(),
        router_address=pair["router"].lower(),
        amount_atomic=12345,tx_hash=HASH)
    tx=dict(hash=HASH,from_=WALLET,to=row["token_address"],
            chainId=hex(pair["chain_id"]),value="0x0",
            input="0x095ea7b3"+f"{int(pair['router'],16):064x}"+f"{12345:064x}",
            blockNumber="0x64",blockHash=BLOCK)
    tx["from"]=tx.pop("from_")
    receipt=dict(transactionHash=HASH,**{
        "from":WALLET,"to":row["token_address"],
        "blockNumber":"0x64","blockHash":BLOCK,"status":"0x1",
    })
    check=lambda t,r,final:_verify_approval_finality(
        row,chain_id=hex(pair["chain_id"]),tx=t,receipt=r,
        finalized={"number":final},
        canonical={"number":"0x64","hash":BLOCK})
    assert check(tx,receipt,"0x63") is None
    assert check(tx,receipt,"0x64")=="FINALIZED_SUCCESS"
    assert _verify_approval_finality(
        {**row,"wallet_address":"0x"+("Ab"*20)},
        chain_id=hex(pair["chain_id"]),tx=tx,receipt=receipt,
        finalized={"number":"0x64"},
        canonical={"number":"0x64","hash":BLOCK})=="FINALIZED_SUCCESS"
    assert _verify_approval_finality(
        row,chain_id=hex(pair["chain_id"]),tx=tx,receipt=receipt,
        finalized={"number":"0x64"},canonical=None) is None
    with pytest.raises(ValueError):
        _verify_approval_finality(
            row,chain_id=hex(pair["chain_id"]),tx=tx,receipt=receipt,
            finalized={"number":"0x64"},
            canonical={"number":"0x64","hash":"0x"+"12"*32})
    assert check(tx,{**receipt,"status":"0x0"},"0x64")=="FINALIZED_REVERTED"
    with pytest.raises(ValueError):
        check({**tx,"input":"0x095ea7b3"+"f"*128},receipt,"0x64")
    with pytest.raises(ValueError):
        check(tx,{**receipt,"blockHash":"0x"+"de"*32},"0x64")
    with pytest.raises(ValueError):
        _verify_approval_finality(
            {**row,"router_address":"0x"+"dc"*20},
            chain_id=hex(pair["chain_id"]),tx=tx,receipt=receipt,
            finalized={"number":"0x64"},
            canonical={"number":"0x64","hash":BLOCK})
    with pytest.raises(ValueError):
        _verify_approval_finality(
            {**row,"token_address":"0x"+"dc"*20},
            chain_id=hex(pair["chain_id"]),tx=tx,receipt=receipt,
            finalized={"number":"0x64"},
            canonical={"number":"0x64","hash":BLOCK})

def test_second_unrelated_market_event_has_independent_approval_nonce():
    args=dict(did="did:privy:user",asset="LINK",side="BUY",
              stable="USDC",wallet=WALLET,amount=123456,revision=5)
    a=_original_approval_id(**args,signal_event_id="market_100")
    b=_original_approval_id(**args,signal_event_id="market_101")
    assert a!=b
    assert a.startswith("appr_") and b.startswith("appr_")



def test_finality_poll_rotates_past_stuck_earliest_approvals(monkeypatch):
    from backend import shared_evm_router_approval_finality as finality
    from backend import shared_evm_router_approval_finality as unit
    records=[{"id":n} for n in range(5)]
    queries=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,args):
            assert "status='SUBMITTED'" in sql
            assert "LIMIT %s OFFSET %s" in sql
            self.limit,self.offset=args
            queries.append(args)
        def fetchall(self):
            return records[self.offset:self.offset+self.limit]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cursor()
    monkeypatch.setattr(unit,"_db_url",lambda:"postgres://unused")
    monkeypatch.setattr(unit,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(unit.psycopg,"connect",lambda *a,**kw:DB())
    monkeypatch.setattr(unit,"_APPROVAL_PAGE_OFFSET",0)
    pages=[finality._pending_approvals(limit=2) for _ in range(4)]
    assert pages==[records[:2],records[2:4],records[4:],records[:2]]
    assert queries==[(2,0),(2,2),(2,4),(2,5),(2,0)]


@pytest.mark.parametrize("asset,stable,mode,value",[
    ("BTC","USDC","THOR_FINITE",50_000_000),
    ("SOL","USDT","THOR_USDT_ZERO",0),
    ("SOL","USDT","THOR_FINITE",50_000_000),
    ("XRP","USDC","THOR_FINITE",50_000_000),
])
def test_native_thor_approval_requires_canonical_receipt_and_exact_spender(
    asset,stable,mode,value,
):
    from backend.live_eth_quotes import STABLES
    router="0x"+"12"*20
    row=dict(
        asset=asset,stable=stable,side="BUY",chain_id=1,
        approval_mode=mode,wallet_address=WALLET,
        token_address=STABLES[stable].lower(),router_address=router,
        amount_atomic=50_000_000,tx_hash=HASH)
    tx=dict(hash=HASH,**{"from":WALLET,"to":STABLES[stable].lower()},
            chainId="0x1",value="0x0",
            input="0x095ea7b3"+f"{int(router,16):064x}"+f"{value:064x}",
            blockNumber="0x64",blockHash=BLOCK)
    receipt=dict(transactionHash=HASH,**{"from":WALLET,
        "to":STABLES[stable].lower()},
        blockNumber="0x64",blockHash=BLOCK,status="0x1")
    kw=dict(chain_id="0x1",tx=tx,receipt=receipt,
            finalized={"number":"0x64"},
            canonical={"number":"0x64","hash":BLOCK})
    assert _verify_approval_finality(row,**kw)=="FINALIZED_SUCCESS"
    assert _verify_approval_finality(row,**{**kw,
        "finalized":{"number":"0x63"}}) is None
    assert _verify_approval_finality(row,**{**kw,
        "receipt":{**receipt,"status":"0x0"}})=="FINALIZED_REVERTED"
    with pytest.raises(ValueError):
        _verify_approval_finality(row,**{**kw,
            "tx":{**tx,"input":"0x095ea7b3"+"f"*128}})
    with pytest.raises(ValueError):
        _verify_approval_finality({**row,"token_address":"0x"+"de"*20},**kw)
    with pytest.raises(ValueError):
        _verify_approval_finality({**row,"chain_id":56},**kw)
    with pytest.raises(ValueError):
        _verify_approval_finality({**row,"approval_mode":"ROUTER02_FINITE"},**kw)


def test_native_xrp_usdt_or_reset_without_usdt_never_finalizes():
    from backend.live_eth_quotes import STABLES
    base=dict(asset="XRP",stable="USDT",side="BUY",chain_id=1,
        approval_mode="THOR_FINITE",wallet_address=WALLET,
        token_address=STABLES["USDT"].lower(),
        router_address="0x"+"12"*20,amount_atomic=50_000_000,tx_hash=HASH)
    with pytest.raises(ValueError):
        _verify_approval_finality(
            base,chain_id="0x1",tx=None,receipt=None,
            finalized=None)
    with pytest.raises(ValueError):
        _verify_approval_finality(
            {**base,"asset":"BTC","approval_mode":"THOR_USDT_ZERO",
             "stable":"USDC","token_address":STABLES["USDC"].lower()},
            chain_id="0x1",tx=None,receipt=None,finalized=None)


def test_native_thor_receipt_scan_is_owner_only_and_separate_from_evm(monkeypatch):
    from backend import shared_evm_router_approval_finality as m
    sqls=[]
    records=[{"asset":"BTC"},{"asset":"SOL"}]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,query,args):
            sqls.append(query)
            assert "approval_mode IN ('THOR_FINITE','THOR_USDT_ZERO')" in query
            self.limit,self.offset=args
        def fetchall(self):return records[self.offset:self.offset+self.limit]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cursor()
    monkeypatch.setattr(m,"_db_url",lambda:"postgres://unused")
    monkeypatch.setattr(m,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(m.psycopg,"connect",lambda *args,**kwargs:DB())
    monkeypatch.setattr(m,"_NATIVE_APPROVAL_PAGE_OFFSET",0)
    assert m._pending_approvals(limit=1,native_only=True)==[records[0]]
    assert m._pending_approvals(limit=1,native_only=True)==[records[1]]
    assert m._pending_approvals(limit=1,native_only=True)==[records[0]]
    assert len(sqls)==4
