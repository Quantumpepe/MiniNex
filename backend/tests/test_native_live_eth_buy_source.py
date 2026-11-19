"""Independent Ethereum BUY source finality for native BTC/SOL/XRP.

One original owner: depositWithExpiry + official stablecoin Transfer + Router
Deposit + canonical Ethereum finalized block. No signing or mainnet spend.
"""
import asyncio
import copy
import inspect as py_inspect

import pytest
from fastapi import HTTPException

from backend import native_live_eth_buy_source as source
from backend import native_live_source_finality as finality
from backend.live_eth_quotes import STABLES
from backend.manual_swap_live_quotes import THOR_ASSETS
from backend.native_coin_balances import _RIPPLE,_checksum

DID="did:privy:eth_original_native_buyer_001"
OWNER="0x"+"a"*40
ROUTER="0x"+"b"*40
VAULT="0x"+"c"*40
HASH="0x"+"d"*64
BLOCK="0x"+"e"*64
_AMOUNT=42500000
ALPHABET="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(raw,alphabet=ALPHABET):
    n=int.from_bytes(raw,"big")
    output=""
    while n:
        n,r=divmod(n,58)
        output=alphabet[r]+output
    return alphabet[0]*(len(raw)-len(raw.lstrip(b"\x00")))+output


_xrp=b"\x00"+bytes([3])*20
NATIVE={
    "BTC":"bc1q"+"q"*38,
    "SOL":b58(bytes([7])*32),
    "XRP":b58(_xrp+_checksum(_xrp),_RIPPLE),
}


def word(n):
    return n.to_bytes(32,"big")


def dynamic_memo(memo,offset):
    body=memo.encode("utf-8")
    return word(offset)+word(len(body))+body+b"\x00"*((-len(body))%32)


def address_word(address):
    return word(int(address,16))


def topic(address):
    return "0x"+address_word(address).hex()


def fixture(asset="BTC",stable="USDC"):
    destination=NATIVE[asset]
    memo="=:"+THOR_ASSETS[asset][0]+":"+destination+"/"+OWNER+":0/1/0"
    token=STABLES[stable]
    body=(address_word(VAULT)+address_word(token)+word(_AMOUNT)
          +dynamic_memo(memo,160)+word(2000000000))
    # ABI dynamic encoding requires its offset in the fourth of five words:
    # replace the constructed dynamic block with 5 canonical head words.
    payload=(address_word(VAULT)+address_word(token)+word(_AMOUNT)
             +word(160)+word(2000000000)+
             word(len(memo.encode("utf-8")))+memo.encode("utf-8")+
             b"\0"*((-len(memo.encode("utf-8")))%32))
    calldata="0x"+(source._DEPOSIT_SELECTOR+payload).hex()
    deposit_event=(
        word(_AMOUNT)+word(64)+word(len(memo.encode("utf-8")))+
        memo.encode("utf-8")+b"\0"*((-len(memo.encode("utf-8")))%32)
    )
    row={
        "privy_did":DID,"asset":asset,"intent_key":"native_"+"f"*64,
        "payload_sha256":"a"*64,"chain":"ethereum-mainnet",
        "side":"BUY","status":"SUBMITTED","stable":stable,
        "input_amount":"42.5","source_wallet":OWNER,
        "destination_wallet":destination,"source_tx_hash":HASH,
    }
    tx={
        "hash":HASH,"from":OWNER,"to":ROUTER,"chainId":"0x1",
        "value":"0x0","blockHash":BLOCK,"blockNumber":"0x64",
        "input":calldata,
    }
    rcpt={
        "transactionHash":HASH,"from":OWNER,"to":ROUTER,
        "status":"0x1","blockHash":BLOCK,"blockNumber":"0x64",
        "gasUsed":"0x5208","effectiveGasPrice":"0x77359400",
        "logs":[{
            "address":token,
            "topics":[source._TRANSFER_TOPIC,topic(OWNER),topic(ROUTER)],
            "data":"0x"+word(_AMOUNT).hex(),
            "transactionHash":HASH,"blockHash":BLOCK,"removed":False,
        },{
            "address":ROUTER,
            "topics":[source._DEPOSIT_TOPIC,topic(VAULT),topic(token)],
            "data":"0x"+deposit_event.hex(),
            "transactionHash":HASH,"blockHash":BLOCK,"removed":False,
        }],
    }
    return {
        "row":row,"native_owner":destination,
        "chain_id":"0x1","transaction":tx,"receipt":rcpt,
        "finalized":{"number":"0x70"},
        "block":{"number":"0x64","hash":BLOCK,
                 "timestamp":hex(1999996400)},
        "inbounds":[{"chain":"ETH","router":ROUTER,"address":VAULT}],
    }


def inspect(case):
    row=case["row"]
    return source.inspect_original_eth_buy_source(
        row,native_owner=case["native_owner"],
        chain_id=case["chain_id"],transaction=case["transaction"],
        receipt=case["receipt"],finalized=case["finalized"],
        block=case["block"],inbounds=case["inbounds"],
    )


@pytest.mark.parametrize("asset,stable",[
    ("BTC","USDC"),("BTC","USDT"),
    ("SOL","USDC"),("SOL","USDT"),
    ("XRP","USDC"),
])
def test_real_original_eth_buy_source_requires_all_chain_proofs(asset,stable):
    result=inspect(fixture(asset,stable))
    assert result["mode"]=="OWNER_ORIGINAL_ETH_BUY_SOURCE_FINALITY"
    assert result["asset"]==asset
    assert result["source_stable"]==stable
    assert result["source_wallet"]==OWNER
    assert result["source_tx_hash"]==HASH
    assert result["source_stable_atomic"]==str(_AMOUNT)
    assert result["source_chain_finalized"] is True
    assert result["trusted_thorchain_deposit_verified"] is True
    assert result["destination_settlement_finalized"] is False
    assert result["fee_assessed"] is False
    assert result["broadcast_or_retry_performed"] is False
    assert len(result["source_proof_sha256"])==64


@pytest.mark.parametrize("tamper",[
    lambda x:x["row"].update(status="PREPARED"),
    lambda x:x["row"].update(side="SELL"),
    lambda x:x["row"].update(chain="bitcoin-mainnet"),
    lambda x:x["row"].update(source_wallet=ROUTER),
    lambda x:x["row"].update(source_tx_hash="0x"+"a"*64),
    lambda x:x["row"].update(destination_wallet=OWNER),
    lambda x:x["row"].update(input_amount="43"),
    lambda x:x["row"].update(asset="DOGE"),
    lambda x:x.update(chain_id="0x38"),
    lambda x:x["transaction"].update(fromAddress=VAULT,**{"from":VAULT}),
    lambda x:x["transaction"].update(hash="0x"+"a"*64),
    lambda x:x["transaction"].update(to=VAULT),
    lambda x:x["transaction"].update(value="0x1"),
    lambda x:x["transaction"].update(input="0x1234"),
    lambda x:x["transaction"].update(blockNumber="0x63"),
    lambda x:x["receipt"].update(status="0x0"),
    lambda x:x["receipt"].update(transactionHash="0x"+"a"*64),
    lambda x:x["receipt"].update(blockHash="0x"+"a"*64),
    lambda x:x["finalized"].update(number="0x63"),
    lambda x:x["block"].update(hash="0x"+"0"*64),
    lambda x:x["block"].update(timestamp="0x77777778"),
    lambda x:x.update(inbounds=[]),
    lambda x:x["inbounds"][0].update(router=VAULT),
    lambda x:x["inbounds"][0].update(address=ROUTER),
    lambda x:x["receipt"]["logs"][0].update(address=STABLES["USDT"]),
    lambda x:x["receipt"]["logs"][0].update(data="0x"+word(_AMOUNT-1).hex()),
    lambda x:x["receipt"]["logs"][0].update(
        topics=[source._TRANSFER_TOPIC,topic(VAULT),topic(ROUTER)]),
    lambda x:x["receipt"]["logs"][0].update(
        topics=[source._TRANSFER_TOPIC,topic(OWNER),topic(VAULT)]),
    lambda x:x["receipt"]["logs"][1].update(
        topics=[source._DEPOSIT_TOPIC,topic(VAULT),topic(VAULT)]),
    lambda x:x["receipt"]["logs"][1].update(data="0x"+word(5).hex()),
    lambda x:x["receipt"]["logs"][0].update(removed=True),
    lambda x:x["receipt"]["logs"].append(copy.deepcopy(x["receipt"]["logs"][1])),
])
def test_invalid_memo_owner_network_vault_or_token_never_proves_buy_spend(tamper):
    case=fixture()
    tamper(case)
    with pytest.raises((ValueError,TypeError)):
        inspect(case)


def test_xrp_disallows_non_usdc_source_stablecoin():
    with pytest.raises(ValueError):
        inspect(fixture("XRP","USDT"))


def test_abi_selector_and_stable_amount_are_byte_exact():
    case=fixture("SOL")
    deposit=source._read_deposit_call(case["transaction"]["input"])
    assert deposit["vault"]==VAULT
    assert deposit["token"]==STABLES["USDC"].lower()
    assert deposit["atomic"]==_AMOUNT
    assert deposit["memo"].startswith("=:SOL.SOL:"+NATIVE["SOL"]+"/"+OWNER)
    assert deposit["expiry"]==2000000000


def test_abi_cannot_masquerade_as_a_different_thor_router():
    case=fixture()
    raw=case["transaction"]["input"]
    with pytest.raises(ValueError):
        source._read_deposit_call("0x"+"deadbeef"+raw[10:])


def test_private_source_reader_never_reads_rpc_for_wrong_privy_owner(monkeypatch):
    case=fixture()
    original=case["row"]
    seen=[]
    class Cur:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,args):seen.append(args)
        def fetchone(self):return original
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cur()
    monkeypatch.setattr(source,"_db_url",lambda:"postgres://unit")
    monkeypatch.setattr(source,"_ensure_schema",lambda x:None)
    monkeypatch.setattr(source.psycopg,"connect",lambda *a,**kw:Conn())
    async def owner(did):return {"id":did}
    monkeypatch.setattr(source,"fetch_privy_user",owner)
    monkeypatch.setattr(source,"_exact_owner_wallets",lambda *a,**k:(OWNER,OWNER))
    with pytest.raises(HTTPException) as error:
        asyncio.run(source.get_original_eth_buy_source(
            DID,asset="BTC",intent_key=original["intent_key"]))
    assert error.value.status_code==409
    assert seen==[(DID,"BTC",original["intent_key"])]


def test_native_buy_eth_source_get_is_authenticated_and_passive():
    from backend.app import app
    endpoints=[r for r in app.routes
               if r.path=="/api/live/native/buy-source/{asset}/{intent_key}"]
    assert len(endpoints)==1
    assert endpoints[0].methods=={"GET"}
    code=py_inspect.getsource(endpoints[0].endpoint)
    assert "verify_privy_bearer" in code
    assert "get_original_eth_buy_source" in code
    assert "no-store" in code


def test_original_eth_buy_source_persists_once_without_buy_fill_or_fee():
    case=fixture("XRP")
    original=case["row"]
    proof=inspect(case)
    selected={
        "status":"SUBMITTED","side":"BUY","chain":"ethereum-mainnet",
        "source_wallet":OWNER,"source_tx_hash":HASH,
        "payload_sha256":original["payload_sha256"],
        "source_proof_sha256":None,
    }
    class Cur:
        def __init__(self):
            self.rows=iter([selected,{"status":"SOURCE_FINALIZED"}])
            self.queries=[]
        def execute(self,sql,args):self.queries.append((sql,args))
        def fetchone(self):return next(self.rows)
    cur=Cur()
    status=finality._persist_source_finality_locked(
        cur,original=original,verified=proof)
    assert status=="SOURCE_FINALIZED"
    assert len(cur.queries)==2
    assert "status='SOURCE_FINALIZED'" in cur.queries[1][0]
    assert "status='SUBMITTED'" in cur.queries[1][0]
    assert cur.queries[1][1][0]==proof["source_proof_sha256"]
    assert proof["fee_assessed"] is False


def test_wrong_source_buy_side_not_saved_as_sell():
    case=fixture()
    original=case["row"]
    verified=inspect(case)
    verified["side"]="SELL"
    class Never:
        def execute(self,*args):
            pytest.fail("Mismatched BUY source must not run SQL")
    with pytest.raises(HTTPException):
        finality._persist_source_finality_locked(
            Never(),original=original,verified=verified)


def test_eth_buy_work_page_keeps_owner_source_identity(monkeypatch):
    originals=[fixture(asset)["row"] for asset in ("BTC","SOL","XRP")]
    calls=[]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,query,params):
            assert "side='BUY'" in query and "status='SUBMITTED'" in query
            self.limit,self.offset=params
            calls.append(params)
        def fetchall(self):return originals[self.offset:self.offset+self.limit]
    monkeypatch.setattr(finality,"_db_url",lambda:"postgres://unit")
    monkeypatch.setattr(finality,"_ensure_schema",lambda x:None)
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:Conn())
    monkeypatch.setattr(finality,"_ETH_BUY_PAGE_OFFSET",0)
    assert finality._pending_original_eth_buy_submissions(2)==originals[:2]
    assert finality._pending_original_eth_buy_submissions(2)==originals[2:]
    assert calls[:2]==[(2,0),(2,2)]
