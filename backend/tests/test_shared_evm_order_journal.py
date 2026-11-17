"""Durable per-asset EVM order journal, no permission or hidden broadcasts."""
import pytest
from fastapi import HTTPException

from backend.shared_evm_order_journal import (
    immutable_evm_send_identity,_claim_locked,_record_result_locked,
)
from backend.shared_evm_swap_receipts import _pair
from backend.shared_evm_execution import EvmPair,unsigned_exact_input_single
from backend.storage import SCHEMA

DID="did:privy:client"
WALLET="0x"+"ab"*20
KEY="sig_abcdef01234567890123456789"
WALLET_ID="wallet_1234567"
DIGEST="a"*64


class Cursor:
    def __init__(self, rows):
        self.rows=iter(rows)
        self.sql=[]
    def execute(self,query,args):
        self.sql.append((query,args))
    def fetchone(self):
        return next(self.rows)


def identity(asset,side="BUY",stable=None):
    stable=stable or ("USDT" if asset=="BNB" else "USDC")
    pair=_pair(asset,stable,side)
    tx=unsigned_exact_input_single(
        pair=EvmPair(asset,pair["chain_id"],pair["network"],
                     pair["source_token"],pair["output_token"],
                     pair["router"],500),
        wallet=WALLET,amount_in=10**6,min_out=12345)
    return immutable_evm_send_identity(
        asset=asset,wallet_id=WALLET_ID,wallet=WALLET,tx=tx,
        side=side,stable=stable,input_atomic="1000000",
        minimum_output_atomic="12345")


@pytest.mark.parametrize("asset,chain",[
    ("BNB",56),("POL",137),("AVAX",43114),("LINK",1),("UNI",1),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_five_evm_assets_have_independent_bound_durable_send_identity(
    asset,chain,side
):
    i=identity(asset,side)
    assert i["chain_id"]==chain
    assert i["wallet_address"]==WALLET
    assert len(i["request_digest"])==64
    assert i["signed_or_sent"] is False
    cur=Cursor([{"privy_did":DID},{"pending":0}] +
               ([{"pending":0},{"pending":0}] if chain==1 else
                [{"pending":0}]))
    _claim_locked(cur,did=DID,key=KEY,identity=i)
    assert len(cur.sql)==(5 if chain==1 else 4)
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "chain_id=%s" in cur.sql[1][0]
    assert cur.sql[1][1]==(DID,chain)
    assert "'SENDING'" in cur.sql[-1][0]
    assert i["request_digest"] in cur.sql[-1][1]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_unresolved_network_order_prevents_duplicate_wallet_send(asset):
    cur=Cursor([{"privy_did":DID},{"pending":1}])
    with pytest.raises(HTTPException) as error:
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert error.value.status_code==409
    assert len(cur.sql)==2


@pytest.mark.parametrize("asset",["LINK","UNI"])
@pytest.mark.parametrize("busy_step",["eth_swap","eth_wrap"])
def test_shared_ethereum_nonce_blocks_erc20_when_eth_operation_pending(asset,busy_step):
    rows=[{"privy_did":DID},{"pending":0}]
    if busy_step=="eth_swap":
        rows.append({"pending":1})
    else:
        rows.extend(({"pending":0},{"pending":1}))
    cur=Cursor(rows)
    with pytest.raises(HTTPException) as err:
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert err.value.status_code==409
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in q for q,a in cur.sql)


@pytest.mark.parametrize("digest,tx_hash,expected",[
    (DIGEST,"0x"+"ab"*32,"SUBMITTED"),
    (DIGEST,None,"RECONCILIATION_REQUIRED"),
])
def test_immutable_send_outcome_never_retries(digest,tx_hash,expected):
    cur=Cursor([{"request_digest":digest,"status":"SENDING"}])
    state=_record_result_locked(
        cur,did=DID,asset="AVAX",key=KEY,
        digest=digest,tx_hash=tx_hash)
    assert state==expected
    assert "'SENDING'" in cur.sql[1][0]
    assert cur.sql[1][1][0]==expected


@pytest.mark.parametrize("row",[
    {"request_digest":DIGEST,"status":"SUBMITTED"},
    {"request_digest":"b"*64,"status":"SENDING"},
    None,
])
def test_claimed_or_conflicting_order_cannot_be_replayed(row):
    cur=Cursor([row])
    with pytest.raises(HTTPException):
        _record_result_locked(
            cur,did=DID,asset="LINK",key=KEY,
            digest=DIGEST,tx_hash="0x"+"ab"*32)
    assert len(cur.sql)==1


def test_eth_legacy_journal_is_not_silently_replaced():
    with pytest.raises(ValueError):
        identity("ETH")
    assert any("CREATE TABLE IF NOT EXISTS mininex_live_evm_auto_broadcasts" in s
               for s in SCHEMA)
    assert any("CREATE TABLE IF NOT EXISTS mininex_live_eth_auto_broadcasts" in s
               for s in SCHEMA)


def test_new_journal_not_accessible_from_public_api():
    from backend.app import app
    assert all("evm-auto-broadcast" not in r.path for r in app.routes)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_unresolved_native_conversion_blocks_same_chain_swap(asset):
    cur=Cursor([{"privy_did":DID},{"pending":0},{"pending":1}])
    with pytest.raises(HTTPException):
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert len(cur.sql)==3
    assert "mininex_live_evm_native_auto_steps" in cur.sql[-1][0]
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in q
                   for q,args in cur.sql)
