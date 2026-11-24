"""No successful native trade or performance fee may be claimed from refund."""
import pytest
from fastapi import HTTPException

from backend import native_live_sell_refund as m
from backend.native_coin_balances import _checksum, _RIPPLE

DID="did:privy:native_refund_owner"
BTC="bc1q"+"q"*38
BTC_TX="d"*64
ALPHABET="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def base58(raw,alphabet=ALPHABET):
    n=int.from_bytes(raw,"big")
    value=""
    while n:
        n,r=divmod(n,58)
        value=alphabet[r]+value
    return alphabet[0]*(len(raw)-len(raw.lstrip(b"\x00")))+value

SOL=base58(bytes([7])*32)
SOL_TX=base58(bytes(range(1,65)))
SOL_SENDER=base58(bytes([9])*32)
payload=bytes([0])+bytes([3])*20
XRP=base58(payload+_checksum(payload),_RIPPLE)
XRP_TX="F"*64
ADDRESSES={"BTC":BTC,"SOL":SOL,"XRP":XRP}
TX={"BTC":BTC_TX,"SOL":SOL_TX,"XRP":XRP_TX}


def original(asset):
    return {
        "privy_did":DID,"asset":asset,"side":"SELL",
        "intent_key":"native_"+"a"*64,
        "source_wallet":ADDRESSES[asset],
        "source_tx_hash":"c"*64 if asset=="BTC" else TX[asset],
        "payload_sha256":"1"*64,"source_proof_sha256":"2"*64,
        "status":"SOURCE_FINALIZED",
    }


def protocol(asset,amount="100000000"):
    row=original(asset)
    return {
        "mode":"THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY",
        "asset":asset,"side":"SELL",
        "source_tx_hash":row["source_tx_hash"],
        "source_wallet_original":True,
        "destination_owner_bound":True,
        "protocol_inbound_observed":True,
        "protocol_inbound_finalized":True,
        "refund_original_source_chain":asset,
        "refund_independently_finalized":False,
        "protocol_outbound_hashes":[],
        "protocol_refund_hashes":[TX[asset]],
        "protocol_refund_units_1e8":[amount],
        "destination_chain_receipts_verified":False,
        "realized_pnl_verified":False,
        "fee_assessed":False,
        "retry_permitted":False,
        "new_transactions_sent":0,
    }


def receipt(asset):
    if asset=="BTC":
        return {
            "mode":"BTC_MAINNET_WALLET_TX_OBSERVATION",
            "asset":"BTC","network":"bitcoin-mainnet",
            "txid":BTC_TX,
            "owned_address_proved_by_privy":True,
            "owner_input_observed":False,"owner_output_observed":True,
            "explorer_reports_confirmed":True,
            "recommended_confirmation_threshold_met":True,
            "confirmations_observed":6,
            "wallet_input_sats":"0","wallet_output_sats":"100000000",
            "crosschain_swap_fill_verified":False,
            "ethereum_stablecoin_settlement_verified":False,
            "execution_enabled":False,"can_submit":False,
        }
    if asset=="SOL":
        return {
            "jsonrpc":"2.0","id":1,
            "result":{
                "slot":1357,
                "transaction":{
                    "signatures":[SOL_TX],
                    "message":{
                        "accountKeys":[
                            {"pubkey":SOL_SENDER,"signer":True,"writable":True},
                            {"pubkey":SOL,"signer":False,"writable":True},
                        ],
                        "instructions":[{
                            "program":"system","parsed":{
                                "type":"transfer",
                                "info":{"source":SOL_SENDER,"destination":SOL,
                                        "lamports":1000000000},
                            },
                        }],
                    },
                },
                "meta":{"err":None,"fee":5000,
                        "preBalances":[3000000000,500],
                        "postBalances":[1999995000,1000000500]},
            },
        }
    return {
        "result":{
            "hash":XRP_TX,"validated":True,"ledger_index":1555,
            "tx_json":{"TransactionType":"Payment","hash":XRP_TX,
                       "Account":"rSource","Destination":XRP,"Fee":"12",
                       "Amount":"1000000","Flags":0},
            "meta":{"TransactionResult":"tesSUCCESS",
                    "delivered_amount":"1000000"},
        },
    }


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_returned_native_coins_are_independently_verified_not_sold(asset):
    proof=m.inspect_original_native_sell_refund(
        original(asset),protocol(asset),receipt(asset))
    assert proof["refund_independently_finalized"] is True
    assert proof["refund_wallet_original"] is True
    assert proof["refund_amount_native"]=="1"
    assert proof["native_sell_settlement_complete"] is False
    assert proof["realized_profit_verified"] is False
    assert proof["fee_assessed"] is False
    assert proof["new_transactions_sent"]==0
    assert proof["can_submit"] is False
    assert proof["retry_permitted"] is False
    assert len(proof["refund_proof_sha256"])==64


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_protocol_claim_without_original_chain_refund_is_never_settled(asset):
    bad=receipt(asset)
    if asset=="BTC":
        bad["owner_output_observed"]=False
    elif asset=="SOL":
        bad["result"]["meta"]["postBalances"][1]-=1
    else:
        bad["result"]["meta"]["delivered_amount"]="999999"
    with pytest.raises(ValueError):
        m.inspect_original_native_sell_refund(original(asset),protocol(asset),bad)


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
@pytest.mark.parametrize("change",[
    lambda p:p.update(protocol_outbound_hashes=["b"*64]),
    lambda p:p.update(protocol_refund_hashes=[]),
    lambda p:p.update(protocol_refund_units_1e8=["0"]),
    lambda p:p.update(protocol_refund_units_1e8=["100000001"]),
    lambda p:p.update(refund_original_source_chain="ETH"),
    lambda p:p.update(source_wallet_original=False),
    lambda p:p.update(protocol_inbound_finalized=False),
    lambda p:p.update(fee_assessed=True),
])
def test_partial_fake_or_wrong_coin_refund_never_closes_original(asset,change):
    p=protocol(asset)
    change(p)
    with pytest.raises(ValueError):
        m.inspect_original_native_sell_refund(original(asset),p,receipt(asset))


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.calls=[]
    def execute(self,sql,params):
        self.calls.append((sql,params))
    def fetchone(self):
        return next(self.rows)


def test_settlement_only_moves_original_sell_once_without_fee():
    original_row=original("BTC")
    proof=m.inspect_original_native_sell_refund(
        original_row,protocol("BTC"),receipt("BTC"))
    cur=Cursor([
        {"status":"SOURCE_FINALIZED","side":"SELL",
         "source_wallet":BTC,
         "source_tx_hash":original_row["source_tx_hash"],
         "payload_sha256":original_row["payload_sha256"],
         "source_proof_sha256":original_row["source_proof_sha256"],
         "destination_tx_hash":None,"destination_proof_sha256":None},
        {"status":"REFUNDED_VERIFIED"},
    ])
    state=m._persist_original_native_sell_refund_locked(
        cur,original=original_row,verified=proof)
    assert state=="REFUNDED_VERIFIED"
    assert len(cur.calls)==2
    assert "WHERE privy_did=%s AND asset=%s AND intent_key=%s" in cur.calls[0][0]
    assert "status='REFUNDED_VERIFIED'" in cur.calls[1][0]
    assert "fee" not in cur.calls[1][0].lower()


def test_duplicate_original_refund_never_updates_second_time():
    original_row=original("BTC")
    proof=m.inspect_original_native_sell_refund(
        original_row,protocol("BTC"),receipt("BTC"))
    cur=Cursor([{
        "status":"REFUNDED_VERIFIED","side":"SELL",
        "source_wallet":BTC,"source_tx_hash":original_row["source_tx_hash"],
        "payload_sha256":original_row["payload_sha256"],
        "source_proof_sha256":original_row["source_proof_sha256"],
        "destination_tx_hash":BTC_TX,
        "destination_proof_sha256":proof["refund_proof_sha256"],
    }])
    with pytest.raises(HTTPException) as err:
        m._persist_original_native_sell_refund_locked(
            cur,original=original_row,verified=proof)
    assert err.value.status_code==409
    assert len(cur.calls)==1


def test_original_refund_cannot_be_recast_as_native_sell_success():
    original_row=original("SOL")
    proof=m.inspect_original_native_sell_refund(
        original_row,protocol("SOL"),receipt("SOL"))
    proof["native_sell_settlement_complete"]=True
    with pytest.raises(HTTPException):
        m._persist_original_native_sell_refund_locked(
            Cursor([]),original=original_row,verified=proof)

def test_original_sell_refund_endpoint_is_authenticated_get_only(monkeypatch):
    import inspect
    from fastapi.testclient import TestClient
    from backend import app as api
    observed=[]
    async def report(did,*,asset,intent_key):
        observed.append((did,asset,intent_key))
        return {"mode":"ORIGINAL_NATIVE_SELL_REFUND_CHAIN_FINALITY",
                "refund_independently_finalized":True,"fee_assessed":False}
    monkeypatch.setattr(api,"get_private_original_native_sell_refund",report)
    monkeypatch.setattr(api,"verify_privy_bearer",
                        lambda authorization:{"user_id":DID} if authorization=="Bearer good" else (_ for _ in ()).throw(HTTPException(401,"Invalid login")))
    path="/api/live/native/sell-refund/BTC/native_"+"a"*64
    client=TestClient(api.app)
    assert client.get(path).status_code==401
    assert client.post(path,headers={"Authorization":"Bearer good"}).status_code==405
    response=client.get(path,headers={"Authorization":"Bearer good"})
    assert response.status_code==200
    assert response.headers["cache-control"]=="private, no-store"
    assert observed==[(DID,"BTC","native_"+"a"*64)]
    endpoint=next(route for route in api.app.routes
                  if route.path=="/api/live/native/sell-refund/{asset}/{intent_key}")
    assert "verify_privy_bearer" in inspect.getsource(endpoint.endpoint)


def test_tron_refund_requires_exact_solidified_native_credit():
    from backend.native_trx_manual_send import tron_address_hex
    from backend.native_trx_user_signed_send import _tron_address_from_public_key
    from eth_keys import keys
    owner=_tron_address_from_public_key(keys.PrivateKey(bytes([1])*32).public_key.to_bytes())
    sender=_tron_address_from_public_key(keys.PrivateKey(bytes([2])*32).public_key.to_bytes())
    row={**original("BTC"),"asset":"TRX","source_wallet":owner}
    tx="d"*64
    proto={**protocol("BTC"),"asset":"TRX","refund_original_source_chain":"TRON",
           "source_tx_hash":row["source_tx_hash"],"protocol_refund_hashes":[tx]}
    observed={"transaction":{"txID":tx,"ret":[{"contractRet":"SUCCESS"}],
        "raw_data":{"contract":[{"type":"TransferContract","parameter":{"value":{
            "owner_address":tron_address_hex(sender),"to_address":tron_address_hex(owner),
            "amount":1000000}}}]}},
        "info":{"id":tx,"blockNumber":42,"receipt":{}}}
    proof=m.inspect_original_native_sell_refund(row,proto,observed)
    assert proof["refund_amount_atomic"]=="1000000"
    assert proof["native_sell_settlement_complete"] is False
    with pytest.raises(ValueError):
        m.inspect_original_native_sell_refund(row,proto,{**observed,"info":{"id":tx}})
