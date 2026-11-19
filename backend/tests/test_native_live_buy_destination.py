"""BTC/SOL/XRP native BUY destination: actual native chain proof, no transfers."""
import asyncio
import copy
import inspect as py_inspect

import pytest
from fastapi import HTTPException

from backend import native_live_buy_destination as buy
from backend.native_coin_balances import _RIPPLE,_checksum

DID="did:privy:verified_native_buyer"
ETH="0x"+"a"*40
BTC="bc1q"+"q"*38
_ALPHABET="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(raw,alphabet=_ALPHABET):
    n=int.from_bytes(raw,"big")
    output=""
    while n:
        n,r=divmod(n,58)
        output=alphabet[r]+output
    return alphabet[0]*(len(raw)-len(raw.lstrip(b"\0")))+output


SOL=b58(bytes([7])*32)
SOL_HASH=b58(bytes(range(1,65)))
SOL_VAULT=b58(bytes([9])*32)
_xrp_payload=b"\x00"+bytes([3])*20
XRP=b58(_xrp_payload+_checksum(_xrp_payload),_RIPPLE)
XRP_HASH="F"*64
BTC_HASH="b"*64

WALLETS={"BTC":BTC,"SOL":SOL,"XRP":XRP}
HASHES={"BTC":BTC_HASH,"SOL":SOL_HASH,"XRP":XRP_HASH}


def original(asset):
    return {
        "privy_did":DID,"asset":asset,"side":"BUY","stable":"USDC",
        "intent_key":"native_"+"d"*64,
        "payload_sha256":"a"*64,
        "source_proof_sha256":"b"*64,
        "status":"SOURCE_FINALIZED",
        "source_wallet":ETH,
        "source_tx_hash":"0x"+"e"*64,
        "destination_wallet":WALLETS[asset],
    }


def protocol(asset):
    return {
        "mode":"THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY",
        "asset":asset,"side":"BUY",
        "source_tx_hash":"0x"+"e"*64,
        "source_wallet_original":True,
        "destination_owner_bound":True,
        "protocol_inbound_observed":True,
        "protocol_inbound_finalized":True,
        "protocol_reports_swap_finalized":True,
        "protocol_outbound_hashes":[HASHES[asset]],
        "protocol_outbound_units_1e8":["100000000"],
        "destination_chain_receipts_verified":False,
        "realized_pnl_verified":False,"fee_assessed":False,
        "retry_permitted":False,"new_transactions_sent":0,
    }


def native(asset):
    if asset=="BTC":
        return {
            "mode":"BTC_MAINNET_WALLET_TX_OBSERVATION",
            "asset":"BTC","network":"bitcoin-mainnet","txid":BTC_HASH,
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
        value=1000000000
        return {
            "jsonrpc":"2.0","id":1,
            "result":{
                "slot":123456,
                "transaction":{
                    "signatures":[SOL_HASH],
                    "message":{
                        "accountKeys":[
                            {"pubkey":SOL_VAULT,"signer":True,"writable":True},
                            {"pubkey":SOL,"signer":False,"writable":True},
                        ],
                        "instructions":[{
                            "program":"system",
                            "parsed":{"type":"transfer","info":{
                                "source":SOL_VAULT,"destination":SOL,
                                "lamports":value}},
                        }],
                    },
                },
                "meta":{"err":None,"fee":5000,
                        "preBalances":[3000000000,100000],
                        "postBalances":[1999995000,100000+value]},
            },
        }
    return {
        "result":{
            "hash":XRP_HASH,"validated":True,"ledger_index":77777,
            "tx_json":{"hash":XRP_HASH,"TransactionType":"Payment",
                       "Account":"rSource","Destination":XRP,
                       "Fee":"12","Amount":"1000000","Flags":0},
            "meta":{"TransactionResult":"tesSUCCESS",
                    "delivered_amount":"1000000"},
        },
    }


@pytest.mark.parametrize("asset,amount",[
    ("BTC","1"),("SOL","1"),("XRP","1"),
])
def test_original_native_buy_reconciles_actual_owner_receipt(asset,amount):
    proof=buy.inspect_original_native_buy_destination(
        original(asset),protocol(asset),native(asset))
    assert proof["mode"]=="OWNER_NATIVE_BUY_DESTINATION_FINALITY"
    assert proof["asset"]==asset
    assert proof["destination_tx_hash"]==HASHES[asset]
    assert proof["destination_received_amount"]==amount
    assert proof["native_buy_settlement_complete"] is True
    assert proof["native_sell_settlement_complete"] is False
    assert proof["trading_profit_verified"] is False
    assert proof["fee_assessed"] is False
    assert proof["can_submit"] is False
    assert proof["retry_permitted"] is False
    assert proof["new_transactions_sent"]==0
    assert len(proof["destination_proof_sha256"])==64


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
@pytest.mark.parametrize("tamper",[
    lambda p:p.update(side="SELL"),
    lambda p:p.update(source_tx_hash="0x"+"f"*64),
    lambda p:p.update(protocol_inbound_finalized=False),
    lambda p:p.update(protocol_reports_swap_finalized=False),
    lambda p:p.update(destination_owner_bound=False),
    lambda p:p.update(protocol_outbound_hashes=[]),
    lambda p:p.update(protocol_outbound_units_1e8=["0"]),
    lambda p:p.update(protocol_outbound_units_1e8=["100000001"]),
    lambda p:p.update(destination_chain_receipts_verified=True),
    lambda p:p.update(fee_assessed=True),
    lambda p:p.update(retry_permitted=True),
])
def test_protocol_only_or_changed_native_outbound_not_received(asset,tamper):
    p=protocol(asset)
    tamper(p)
    with pytest.raises((ValueError,TypeError)):
        buy.inspect_original_native_buy_destination(original(asset),p,native(asset))


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
@pytest.mark.parametrize("tamper",[
    lambda r:r.update(status="SUBMITTED"),
    lambda r:r.update(side="SELL"),
    lambda r:r.update(source_proof_sha256=None),
    lambda r:r.update(destination_wallet=ETH),
    lambda r:r.update(privy_did=None),
])
def test_unfinalized_or_wrong_original_buy_is_not_a_native_credit(asset,tamper):
    row=original(asset)
    tamper(row)
    with pytest.raises((ValueError,TypeError)):
        buy.inspect_original_native_buy_destination(row,protocol(asset),native(asset))


@pytest.mark.parametrize("change",[
    lambda r:r.update(wallet_output_sats="99999999"),
    lambda r:r.update(owner_input_observed=True),
    lambda r:r.update(explorer_reports_confirmed=False),
    lambda r:r.update(recommended_confirmation_threshold_met=False),
    lambda r:r.update(confirmations_observed=5),
    lambda r:r.update(txid="0"*64),
    lambda r:r.update(owned_address_proved_by_privy=False),
])
def test_bitcoin_rejects_unconfirmed_wrong_owner_or_wrong_quantity(change):
    r=native("BTC")
    change(r)
    with pytest.raises(ValueError):
        buy.inspect_original_native_buy_destination(original("BTC"),protocol("BTC"),r)


@pytest.mark.parametrize("change",[
    lambda r:r["result"].update(slot=0),
    lambda r:r["result"]["meta"].update(err={"InstructionError":[0,"Bad"]}),
    lambda r:r["result"]["transaction"].update(signatures=["bad"]),
    lambda r:r["result"]["transaction"]["message"]["accountKeys"][1].update(signer=True),
    lambda r:r["result"]["transaction"]["message"]["accountKeys"][1].update(writable=False),
    lambda r:r["result"]["meta"].update(postBalances=[1999995000,500000]),
    lambda r:r["result"]["transaction"]["message"]["instructions"][0]["parsed"]["info"].update(destination=SOL_VAULT),
    lambda r:r["result"]["transaction"]["message"]["instructions"][0]["parsed"]["info"].update(lamports=99999999),
    lambda r:r["result"]["transaction"]["message"]["instructions"][0].update(program="other"),
    lambda r:r["result"]["transaction"]["message"]["instructions"].append({"program":"system"}),
])
def test_solana_requires_exact_finalized_system_transfer_to_user(change):
    r=native("SOL")
    change(r)
    with pytest.raises((ValueError,TypeError)):
        buy.inspect_original_native_buy_destination(original("SOL"),protocol("SOL"),r)


@pytest.mark.parametrize("change",[
    lambda r:r["result"].update(validated=False),
    lambda r:r["result"].update(ledger_index=0),
    lambda r:r["result"]["tx_json"].update(hash="0"*64),
    lambda r:r["result"]["tx_json"].update(Destination="rAnother"),
    lambda r:r["result"]["tx_json"].update(Account=XRP),
    lambda r:r["result"]["tx_json"].update(Flags=131072),
    lambda r:r["result"]["tx_json"].update(TransactionType="OfferCreate"),
    lambda r:r["result"]["tx_json"].update(Amount="900000"),
    lambda r:r["result"]["meta"].update(delivered_amount="900000"),
    lambda r:r["result"]["meta"].update(TransactionResult="tecUNFUNDED_PAYMENT"),
])
def test_xrp_requires_verified_exact_native_payment_delivery(change):
    r=native("XRP")
    change(r)
    with pytest.raises((ValueError,TypeError)):
        buy.inspect_original_native_buy_destination(original("XRP"),protocol("XRP"),r)


def test_xrp_declines_sub_drop_thor_amount():
    p=protocol("XRP")
    p["protocol_outbound_units_1e8"]=["100000001"]
    with pytest.raises(ValueError):
        buy.inspect_original_native_buy_destination(original("XRP"),p,native("XRP"))


def test_no_owner_identity_means_no_original_protocol_or_rpc(monkeypatch):
    row=original("SOL")
    trace=[]
    class Cur:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,q,args):trace.append(args)
        def fetchone(self):return row
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cur()
    monkeypatch.setattr(buy,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(buy,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(buy.psycopg,"connect",lambda *a,**k:Conn())
    async def privy(did):return {"id":did}
    monkeypatch.setattr(buy,"fetch_privy_user",privy)
    monkeypatch.setattr(buy,"_exact_owner_wallets",lambda *a,**k:(ETH,ETH))
    async def forbid(*args,**kwargs):
        pytest.fail("Wrong Privy native account must not query protocol")
    monkeypatch.setattr(buy,"get_private_original_protocol_status",forbid)
    with pytest.raises(HTTPException) as e:
        asyncio.run(buy.get_private_original_native_buy_destination(
            DID,asset="SOL",intent_key=row["intent_key"]))
    assert e.value.status_code==409
    assert trace==[(DID,"SOL",row["intent_key"])]


def test_native_buy_settlement_get_auth_and_read_only():
    from backend.app import app
    matches=[r for r in app.routes
             if r.path=="/api/live/native/buy-settlement/{asset}/{intent_key}"]
    assert len(matches)==1
    assert matches[0].methods=={"GET"}
    code=py_inspect.getsource(matches[0].endpoint)
    assert "verify_privy_bearer" in code
    assert "get_private_original_native_buy_destination" in code
    assert "no-store" in code
    assert "send_transaction" not in code.lower()
    assert "post(" not in code.lower()


def test_native_buy_source_must_precede_destination_read():
    class C:
        def execute(self,sql,args):pass
        def fetchone(self):
            x=original("BTC")
            x["status"]="SUBMITTED"
            return x
    with pytest.raises(HTTPException) as e:
        buy._read_original_buy_locked(C(),did=DID,asset="BTC",key=original("BTC")["intent_key"])
    assert e.value.status_code==409


class BuyFinalizeCursor:
    def __init__(self,row):
        self.rows=iter([row,{"status":"DESTINATION_FINALIZED"}])
        self.queries=[]
    def execute(self,sql,args):
        self.queries.append((sql,args))
    def fetchone(self):
        return next(self.rows)


def _persisted_native_buy_row(row):
    return {
        "status":"SOURCE_FINALIZED","side":"BUY","stable":row["stable"],
        "source_tx_hash":row["source_tx_hash"],
        "payload_sha256":row["payload_sha256"],
        "source_proof_sha256":row["source_proof_sha256"],
        "source_wallet":row["source_wallet"],
        "destination_wallet":row["destination_wallet"],
        "destination_tx_hash":None,
        "destination_proof_sha256":None,
    }


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_exact_original_native_buy_is_durably_closed_once(asset):
    row=original(asset)
    proof=buy.inspect_original_native_buy_destination(row,protocol(asset),native(asset))
    cur=BuyFinalizeCursor(_persisted_native_buy_row(row))
    result=buy._persist_original_native_buy_locked(
        cur,original=row,verified=proof)
    assert result=="DESTINATION_FINALIZED"
    assert len(cur.queries)==2
    statement,args=cur.queries[1]
    assert "status='DESTINATION_FINALIZED'" in statement
    assert "side='BUY'" in statement
    assert "status='SOURCE_FINALIZED'" in statement
    assert "destination_proof_sha256 IS NULL" in statement
    assert args[:3]==(
        proof["destination_tx_hash"],
        proof["destination_proof_sha256"],
        proof["destination_received_atomic"],
    )
    assert args[3:6]==(DID,asset,row["intent_key"])
    assert args[6:]==(row["source_tx_hash"],row["source_proof_sha256"])


@pytest.mark.parametrize("alter",[
    lambda r:r.update(status="DESTINATION_FINALIZED"),
    lambda r:r.update(side="SELL"),
    lambda r:r.update(source_tx_hash="0x"+"0"*64),
    lambda r:r.update(source_proof_sha256="f"*64),
    lambda r:r.update(source_wallet="0x"+"0"*40),
    lambda r:r.update(destination_wallet="0x"+"0"*40),
    lambda r:r.update(destination_tx_hash="a"*64),
    lambda r:r.update(destination_proof_sha256="d"*64),
])
def test_native_buy_immutable_journal_rejects_changed_or_settled_row(alter):
    row=original("BTC")
    proof=buy.inspect_original_native_buy_destination(row,protocol("BTC"),native("BTC"))
    saved=_persisted_native_buy_row(row)
    alter(saved)
    cur=BuyFinalizeCursor(saved)
    with pytest.raises(HTTPException):
        buy._persist_original_native_buy_locked(cur,original=row,verified=proof)
    assert len(cur.queries)==1


@pytest.mark.parametrize("change",[
    {"native_buy_settlement_complete":False},
    {"native_sell_settlement_complete":True},
    {"source_tx_hash":"0x"+"f"*64},
    {"original_source_proof_sha256":"f"*64},
    {"destination_wallet_original":False},
    {"destination_received_atomic":"0"},
    {"destination_received_amount":"9999"},
    {"destination_proof_sha256":None},
    {"fee_assessed":True},
    {"trading_profit_verified":True},
    {"new_transactions_sent":1},
    {"retry_permitted":True},
])
def test_native_buy_denies_forged_payout_duplicate_or_fee_claim(change):
    row=original("SOL")
    proof=buy.inspect_original_native_buy_destination(row,protocol("SOL"),native("SOL"))
    proof.update(change)
    cur=BuyFinalizeCursor(_persisted_native_buy_row(row))
    with pytest.raises((ValueError,HTTPException)):
        buy._persist_original_native_buy_locked(cur,original=row,verified=proof)
    assert cur.queries==[]
