"""Native Solana/XRPL confirmed sender evidence; NEVER crosschain payout proof."""
import asyncio
import pytest
from fastapi import HTTPException
from backend import native_trade_receipts as receipts
from backend import native_coin_balances as native

DID="did:privy:owner_native_123"
ALPHABET="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58(raw,alphabet=ALPHABET):
    n=int.from_bytes(raw,"big")
    out=""
    while n:
        n,r=divmod(n,58)
        out=alphabet[r]+out
    return alphabet[0]*(len(raw)-len(raw.lstrip(b"\x00")))+out

SOL=b58(bytes([7])*32)
SIG=b58(bytes(range(1,65)))
XRP_PAYLOAD=bytes([0])+bytes([3])*20
XRP=b58(XRP_PAYLOAD+native._checksum(XRP_PAYLOAD),native._RIPPLE)
HASH="F"*64


def sol_receipt(owner=SOL,signature=SIG):
    return {"jsonrpc":"2.0","id":1,"result":{
        "slot":12345,"transaction":{
            "signatures":[signature],
            "message":{"accountKeys":[
                {"pubkey":owner,"signer":True,"writable":True},
                {"pubkey":SOL,"signer":False,"writable":False}
            ]}},
        "meta":{"err":None,"fee":5000,
                "preBalances":[2000000000,0],"postBalances":[1499995000,0]},
    }}


def xrp_receipt(owner=XRP,tx_hash=HASH):
    return {"result":{
        "hash":tx_hash,"validated":True,"ledger_index":55555,
        "tx_json":{"TransactionType":"Payment","Account":owner,
                   "Destination":owner,"Fee":"12"},
        "meta":{"TransactionResult":"tesSUCCESS"},
    }}


def test_solana_real_confirmed_wallet_signer_balance_and_fee():
    data=receipts.inspect_finalized_solana_sender(SOL,SIG,sol_receipt())
    assert data["asset"]=="SOL"
    assert data["source_chain_finalized"] is True
    assert data["network_fee_lamports"]=="5000"
    assert data["source_wallet_balance_before_lamports"]=="2000000000"
    assert data["source_wallet_balance_after_lamports"]=="1499995000"
    assert data["cross_chain_swap_completed"] is False


@pytest.mark.parametrize("tamper",[
    lambda x:x["result"].update(slot=None),
    lambda x:x["result"]["meta"].update(err={"InstructionError":[0,"Bad"]}),
    lambda x:x["result"]["transaction"].update(signatures=[b58(bytes([1])*64)]),
    lambda x:x["result"]["transaction"]["message"]["accountKeys"][0].update(signer=False),
    lambda x:x["result"]["meta"].update(postBalances=[]),
    lambda x:x["result"]["meta"].update(fee=-1),
])
def test_solana_ambiguous_transaction_cannot_claim_original_finality(tamper):
    raw=sol_receipt()
    tamper(raw)
    with pytest.raises(ValueError):
        receipts.inspect_finalized_solana_sender(SOL,SIG,raw)


def test_validated_xrp_payment_belongs_to_privy_owner_only():
    row=receipts.inspect_validated_xrp_sender(XRP,HASH,xrp_receipt())
    assert row["source_chain_finalized"] is True
    assert row["network_fee_drops"]=="12"
    assert row["cross_chain_swap_completed"] is False
    assert row["destination_stablecoin_received"] is False


@pytest.mark.parametrize("tamper",[
    lambda x:x["result"].update(validated=False),
    lambda x:x["result"].update(hash="A"*64),
    lambda x:x["result"]["tx_json"].update(Account="rOther"),
    lambda x:x["result"]["tx_json"].update(TransactionType="OfferCreate"),
    lambda x:x["result"]["meta"].update(TransactionResult="tecUNFUNDED_PAYMENT"),
    lambda x:x["result"].update(ledger_index=0),
    lambda x:x["result"]["tx_json"].update(Fee="bad"),
])
def test_xrp_rejected_or_unvalidated_is_never_a_completed_sender_receipt(tamper):
    raw=xrp_receipt()
    tamper(raw)
    with pytest.raises(ValueError):
        receipts.inspect_validated_xrp_sender(XRP,HASH,raw)


def test_native_receipt_endpoint_owns_sender_before_using_rpc(monkeypatch):
    calls=[]
    async def user(did):
        calls.append(("privy",did))
        return {"id":did,"linked_accounts":[]}
    monkeypatch.setattr(receipts,"fetch_privy_user",user)
    monkeypatch.setattr(receipts,"verified_native_addresses",
                        lambda user,did:{"SOL":None,"XRP":None})
    with pytest.raises(HTTPException) as err:
        asyncio.run(receipts.get_owner_native_sender_receipt(
            DID,asset="XRP",transaction_hash=HASH))
    assert err.value.status_code==409
    assert calls==[("privy",DID)]


def test_unknown_native_hash_cannot_invoke_privy_or_rpc(monkeypatch):
    async def no_privy(*args):
        pytest.fail("Invalid transaction hash must fail before any Privy call")
    monkeypatch.setattr(receipts,"fetch_privy_user",no_privy)
    with pytest.raises(HTTPException) as err:
        asyncio.run(receipts.get_owner_native_sender_receipt(
            DID,asset="SOL",transaction_hash="0x"+"0"*64))
    assert err.value.status_code==422


def test_owner_xrpl_receipt_checks_real_validated_rpc_only(monkeypatch):
    payloads=[]
    async def user(did):return {"id":did,"linked_accounts":[]}
    monkeypatch.setattr(receipts,"fetch_privy_user",user)
    monkeypatch.setattr(receipts,"verified_native_addresses",
                        lambda user,did:{"XRP":XRP})
    class Response:
        content=b"{}"
        def raise_for_status(self):pass
        def json(self):return xrp_receipt()
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            payloads.append((url,kwargs["json"]))
            return Response()
    monkeypatch.setattr(receipts.httpx,"AsyncClient",Client)
    result=asyncio.run(receipts.get_owner_native_sender_receipt(
        DID,asset="XRP",transaction_hash=HASH))
    assert result["source_chain_finalized"] is True
    assert result["trading_profit_recorded"] is False
    assert result["fee_assessed"] is False
    assert result["can_submit"] is False
    assert payloads[0][1]["method"]=="tx"
    assert payloads[0][1]["params"][0]["transaction"]==HASH
