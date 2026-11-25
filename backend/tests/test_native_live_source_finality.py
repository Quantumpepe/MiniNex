"""Native source finality never implies THORChain payout or a 5% fee."""
import asyncio

import pytest
from fastapi import HTTPException

from backend import native_live_source_finality as finality
from backend.storage import SCHEMA

DID="did:privy:owner_source"
BTC_ADDRESS="bc1q"+"q"*38
SOL_ADDRESS="7"*44
XRP_ADDRESS="r"+"a"*33
BTC_HASH="a"*64
XRP_HASH="F"*64

def b58(value,alphabet="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"):
    n=int.from_bytes(value,"big")
    result=""
    while n:
        n,digit=divmod(n,58)
        result=alphabet[digit]+result
    return alphabet[0]*(len(value)-len(value.lstrip(b"\x00")))+result

SOL_HASH=b58(bytes(range(1,65)))
WALLET={"BTC":BTC_ADDRESS,"SOL":SOL_ADDRESS,"XRP":XRP_ADDRESS}
TX={"BTC":BTC_HASH,"SOL":SOL_HASH,"XRP":XRP_HASH}
NETWORK={"BTC":"bitcoin-mainnet","SOL":"solana-mainnet","XRP":"xrpl-mainnet"}


def original(asset="BTC"):
    return {
        "privy_did":DID,"asset":asset,
        "intent_key":"native_"+"e"*64,
        "payload_sha256":"a"*64,
        "side":"SELL",
        "chain":NETWORK[asset],
        "source_wallet":WALLET[asset],
        "source_tx_hash":TX[asset],
        "status":"SUBMITTED",
    }


def proof(asset):
    if asset=="BTC":
        return {
            "mode":"BTC_MAINNET_WALLET_TX_OBSERVATION",
            "asset":"BTC","network":"bitcoin-mainnet",
            "txid":BTC_HASH,"owned_address_proved_by_privy":True,
            "owner_input_observed":True,"explorer_reports_confirmed":True,
            "recommended_confirmation_threshold_met":True,
            "confirmations_observed":6,
            "wallet_input_sats":"1000000",
            "wallet_output_sats":"10000","transaction_fee_btc":"0.00002",
            "crosschain_swap_fill_verified":False,
            "ethereum_stablecoin_settlement_verified":False,
            "wallet_signed":False,"can_submit":False,
        }
    data={
        "mode":"OWNER_NATIVE_SOURCE_RECEIPT_READ_ONLY",
        "asset":asset,"network":NETWORK[asset],
        "source_tx_hash":TX[asset],
        "source_chain_finalized":True,
        "destination_finality_still_required":True,
        "cross_chain_swap_completed":False,
        "destination_stablecoin_received":False,
        "trading_profit_recorded":False,
        "fee_assessed":False,"can_submit":False,
    }
    if asset=="SOL":
        data.update(source_wallet_signature_verified=True,
                    source_chain_slot=56000,network_fee_lamports="5000")
    else:
        data.update(source_wallet_transaction_verified=True,
                    source_chain_ledger=60000,network_fee_drops="12")
    return data


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_original_chain_finality_is_not_a_crosschain_swap(asset):
    row=original(asset)
    verified=finality._confirmed_original_source(row,proof(asset),WALLET[asset])
    assert verified["source_chain_finalized"] is True
    assert verified["destination_settlement_finalized"] is False
    assert verified["net_profit_verified"] is False
    assert verified["fee_assessed"] is False
    assert verified["broadcast_or_retry_performed"] is False
    assert verified["source_tx_hash"]==TX[asset]
    assert verified["original_order_sha256"]==row["payload_sha256"]
    assert len(verified["source_proof_sha256"])==64


@pytest.mark.parametrize("asset,mutate",[
    ("BTC",{"owner_input_observed":False}),
    ("BTC",{"confirmations_observed":5}),
    ("BTC",{"recommended_confirmation_threshold_met":False}),
    ("BTC",{"explorer_reports_confirmed":False}),
    ("BTC",{"ethereum_stablecoin_settlement_verified":True}),
    ("SOL",{"source_wallet_signature_verified":False}),
    ("SOL",{"source_chain_finalized":False}),
    ("SOL",{"source_chain_slot":0}),
    ("SOL",{"source_tx_hash":b58(bytes([2])*64)}),
    ("XRP",{"source_wallet_transaction_verified":False}),
    ("XRP",{"source_chain_ledger":0}),
    ("XRP",{"destination_stablecoin_received":True}),
    ("XRP",{"fee_assessed":True}),
    ("XRP",{"network_fee_drops":"NaN"}),
])
def test_unverified_or_misbound_original_chain_never_finalizes(asset,mutate):
    data=proof(asset)
    data.update(mutate)
    with pytest.raises(ValueError):
        finality._confirmed_original_source(original(asset),data,WALLET[asset])


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_changed_owner_or_replayed_buy_cannot_claim_sell_finality(asset):
    row=original(asset)
    with pytest.raises(ValueError):
        finality._confirmed_original_source(
            row,proof(asset),"not_the_privy_owner")
    row["side"]="BUY"
    with pytest.raises(ValueError):
        finality._confirmed_original_source(
            row,proof(asset),WALLET[asset])


class Cursor:
    def __init__(self,items):
        self.rows=iter(items)
        self.queries=[]
    def execute(self,sql,params):
        self.queries.append((sql,params))
    def fetchone(self):
        return next(self.rows)


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_original_source_proof_committed_with_journal_and_only_once(asset):
    row=original(asset)
    evidence=finality._confirmed_original_source(row,proof(asset),WALLET[asset])
    original_row={k:row[k] for k in (
        "status","side","chain","source_wallet","source_tx_hash",
        "payload_sha256")}
    original_row["source_proof_sha256"]=None
    fee={"BTC":"2000","SOL":"5000","XRP":"12"}[asset]
    assert evidence["checkpoint"]["network_fee_atomic"]==fee
    cur=Cursor([original_row,{"status":"SOURCE_FINALIZED"},{"source_network_fee_atomic":fee}])
    result=finality._persist_source_finality_locked(
        cur,original=row,verified=evidence)
    assert result=="SOURCE_FINALIZED"
    assert len(cur.queries)==3
    assert "source_network_fee_atomic" in cur.queries[2][0]
    assert cur.queries[2][1][0]==fee
    assert "FOR UPDATE" in cur.queries[0][0]
    assert "source_proof_sha256" in cur.queries[1][0]
    assert cur.queries[1][1][0]==evidence["source_proof_sha256"]
    assert "status='SUBMITTED'" in cur.queries[1][0]
    again=Cursor([{**original_row,"status":"SOURCE_FINALIZED",
                  "source_proof_sha256":evidence["source_proof_sha256"]}])
    with pytest.raises(HTTPException):
        finality._persist_source_finality_locked(
            again,original=row,verified=evidence)
    assert len(again.queries)==1


def test_mutated_receipt_economics_cannot_be_written_to_ledger():
    row=original("BTC")
    evidence=finality._confirmed_original_source(
        row,proof("BTC"),WALLET["BTC"])
    evidence["destination_settlement_finalized"]=True
    cur=Cursor([])
    with pytest.raises(HTTPException):
        finality._persist_source_finality_locked(
            cur,original=row,verified=evidence)
    assert cur.queries==[]


def test_finality_page_does_not_skip_submitted_rows_after_prior_rows_settle(monkeypatch):
    from datetime import datetime,timezone,timedelta
    base=datetime(2026,11,21,tzinfo=timezone.utc)
    entries=[original(asset)|{
        "created_at":base+timedelta(minutes=i),
        "intent_key":"native_"+f"{i:064x}",
    } for i,asset in enumerate(("BTC","SOL","XRP","BTC","SOL"))]
    queries=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,query,params):
            assert "side='SELL'" in query
            assert "status='SUBMITTED'" in query
            assert "LIMIT %s" in query
            assert "OFFSET" not in query
            self.limit=params[-1]
            self.after=params[:-1] if len(params)>1 else None
            queries.append(params)
        def fetchall(self):
            candidates=entries
            if self.after is not None:
                candidates=[row for row in entries if (
                    row["created_at"],row["privy_did"],row["asset"],
                    row["intent_key"])>tuple(self.after)]
            return candidates[:self.limit]
    monkeypatch.setitem(finality._RECEIPT_PAGE_CURSORS,"sell",None)
    monkeypatch.setattr(finality,"_db_url",lambda:"postgres://test.invalid")
    monkeypatch.setattr(finality,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:DB())
    first=finality._pending_original_source_submissions(limit=2)
    assert first==entries[:2]
    del entries[:2]
    second=finality._pending_original_source_submissions(limit=2)
    assert second==entries[:2]
    del entries[:2]
    last=finality._pending_original_source_submissions(limit=2)
    assert last==entries[:1]
    again=finality._pending_original_source_submissions(limit=2)
    assert again==entries[:1]
    assert len(queries)==5
    assert len(queries[0])==1
    assert len(queries[1])==5
    assert len(queries[2])==5
    assert len(queries[3])==5
    assert len(queries[4])==1


def test_reconcile_reads_owner_and_never_signs_or_broadcasts(monkeypatch):
    row=original("SOL")
    monkeypatch.setattr(finality,"_pending_original_source_submissions",
                        lambda limit=32:[row])
    calls=[]
    async def user(did):
        calls.append(("privy",did))
        return {"id":did}
    monkeypatch.setattr(finality,"fetch_privy_user",user)
    monkeypatch.setattr(finality,"_exact_owner_wallets",
                        lambda user,did,asset:(SOL_ADDRESS,"0x"+"a"*40))
    async def native(did,*,asset,transaction_hash):
        calls.append(("rpc",asset,transaction_hash))
        return proof("SOL")
    monkeypatch.setattr(finality,"get_owner_native_sender_receipt",native)
    monkeypatch.setattr(finality,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(finality,"_ensure_schema",lambda _:None)
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:DB())
    def store(cur,*,original,verified):
        calls.append(("store",original["intent_key"],verified["source_proof_sha256"]))
        return "SOURCE_FINALIZED"
    monkeypatch.setattr(finality,"_persist_source_finality_locked",store)
    result=asyncio.run(finality.reconcile_original_native_source_once())
    assert [x[0] for x in calls]==["privy","rpc","store"]
    assert result["finalized_sources"]==1
    assert result["new_transactions_sent"]==0
    assert result["fees_paid"] is False


def test_sql_has_immutable_chain_proof_not_native_trading_authority():
    schema="\n".join(SCHEMA)
    assert "source_proof_sha256" in schema
    assert "source_finalized_at" in schema
    assert "SOURCE_FINALIZED" in schema
    assert "CREATE UNIQUE INDEX IF NOT EXISTS mininex_native_original_source_hash_unique" in schema


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
@pytest.mark.parametrize("initial_state",["SENDING","RECONCILIATION_REQUIRED"])
def test_unknown_signed_source_can_finish_only_if_original_hash_is_confirmed(asset,initial_state):
    row=original(asset)
    expected=row.pop("source_tx_hash")
    row["status"]=initial_state
    row["source_tx_hash"]=None
    row["source_signed_tx_hash"]=expected
    temporary={**row,"status":"SUBMITTED","source_tx_hash":expected}
    verified=finality._confirmed_original_source(
        temporary,proof(asset),WALLET[asset],
    )
    db_row={k:temporary[k] for k in (
        "status","side","chain","source_wallet","source_tx_hash",
        "payload_sha256")}
    db_row["source_proof_sha256"]=None
    cur=Cursor([
        {"status":"SUBMITTED"},db_row,{"status":"SOURCE_FINALIZED"},
        {"source_network_fee_atomic":verified["checkpoint"]["network_fee_atomic"]},
    ])
    actual=finality._persist_unknown_native_finality_locked(
        cur,original=row,verified=verified,
    )
    assert actual=="SOURCE_FINALIZED"
    assert len(cur.queries)==4
    assert "source_network_fee_atomic" in cur.queries[3][0]
    assert "AND status=%s" in cur.queries[0][0]
    assert "source_signed_tx_hash=%s" in cur.queries[0][0]
    assert cur.queries[0][1][3]==initial_state
    assert cur.queries[0][1][4]==expected
    assert "status='SUBMITTED'" in cur.queries[2][0]


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
@pytest.mark.parametrize("initial_state",["SENDING","RECONCILIATION_REQUIRED"])
def test_unknown_chain_source_without_exact_proof_cannot_promote(asset,initial_state):
    row=original(asset)
    expected=row.pop("source_tx_hash")
    row["status"]=initial_state
    row["source_tx_hash"]=None
    row["source_signed_tx_hash"]=expected
    temporary={**row,"status":"SUBMITTED","source_tx_hash":expected}
    verified=finality._confirmed_original_source(
        temporary,proof(asset),WALLET[asset])
    for changed in (
        {"source_chain_finalized":False},
        {"source_tx_hash":"0"*64},
        {"source_tx_hash":None},
    ):
        cur=Cursor([])
        with pytest.raises(HTTPException):
            finality._persist_unknown_native_finality_locked(
                cur,original=row,verified={**verified,**changed})
        assert cur.queries==[]


def test_unknown_source_recovery_never_rebroadcasts_even_on_rpc_failure(monkeypatch):
    row=original("BTC")
    expected=row.pop("source_tx_hash")
    row["status"]="RECONCILIATION_REQUIRED"
    row["source_tx_hash"]=None
    row["source_signed_tx_hash"]=expected
    monkeypatch.setattr(finality,"_pending_unknown_signed_sources",
                        lambda limit=32:[row])
    calls=[]
    async def user(did):
        calls.append("owner")
        return {"id":did}
    async def check(did,hash_):
        calls.append(("btc_receipt",hash_))
        raise HTTPException(503,"Explorer temporarily unavailable")
    monkeypatch.setattr(finality,"fetch_privy_user",user)
    monkeypatch.setattr(finality,"_exact_owner_wallets",
                        lambda u,d,a:(WALLET[a],"0x"+"a"*40))
    monkeypatch.setattr(finality,"check_native_btc_transaction",check)
    result=asyncio.run(
        finality.reconcile_unknown_original_native_signed_sources_once())
    assert result["finalized_without_resend"]==0
    assert result["unresolved_without_resend"]==1
    assert result["new_signatures_created"]==0
    assert result["new_transactions_sent"]==0
    assert result["source_resubmission_permitted"] is False
    assert calls==["owner",("btc_receipt",expected)]


def test_unknown_recovery_reuses_original_transaction_id_not_new_swap():
    import inspect
    source=inspect.getsource(
        finality.reconcile_unknown_original_native_signed_sources_once)
    assert 'signed_hash=original["source_signed_tx_hash"]' in source
    for prohibited in ("sendTransaction(","broadcast_original_native_signed_once(",
                       "sign_transaction(","eth_sendTransaction(","submit("):
        assert prohibited not in source


def test_passive_recovery_scans_stranded_sending_without_retry(monkeypatch):
    queries=[]
    class DB:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def cursor(self,**kwargs): return self
        def execute(self,sql,parameters):
            queries.append((sql,parameters))
        def fetchall(self):
            return []
    monkeypatch.setitem(finality._RECEIPT_PAGE_CURSORS,"unknown",None)
    monkeypatch.setattr(finality,"_db_url",lambda:"postgres://fake")
    monkeypatch.setattr(finality,"_ensure_schema",lambda *_:None)
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:DB())
    assert finality._pending_unknown_signed_sources(limit=2)==[]
    sql,params=queries[0]
    assert "status='RECONCILIATION_REQUIRED'" in sql
    assert "status='SENDING'" in sql
    assert "updated_at < now() - interval '60 seconds'" in sql
    assert "source_signed_tx_hash IS NOT NULL" in sql
    assert "source_tx_hash IS NULL" in sql
    assert "side='SELL'" in sql
    assert params==(2,)


def test_crashed_sending_receipt_finalizes_without_second_send(monkeypatch):
    row=original("SOL")
    tx=row["source_tx_hash"]
    row.update(status="SENDING",source_tx_hash=None,
               source_signed_tx_hash=tx)
    monkeypatch.setattr(finality,"_pending_unknown_signed_sources",
                        lambda limit=32:[row])
    observed=[]
    async def user(did):
        observed.append("owner")
        return {"id":did}
    async def native(did,*,asset,transaction_hash):
        observed.append(("finalized_source",asset,transaction_hash))
        return proof("SOL")
    monkeypatch.setattr(finality,"fetch_privy_user",user)
    monkeypatch.setattr(finality,"_exact_owner_wallets",
                        lambda user,did,asset:(WALLET[asset],"0x"+"a"*40))
    monkeypatch.setattr(finality,"get_owner_native_sender_receipt",native)
    monkeypatch.setattr(finality,"_db_url",lambda:"postgres://fake")
    monkeypatch.setattr(finality,"_ensure_schema",lambda *_:None)
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:DB())
    def persist(cursor,*,original,verified):
        observed.append(("persist",original["status"],
                         verified["source_tx_hash"]))
        assert original["status"]=="SENDING"
        assert verified["source_chain_finalized"] is True
        return "SOURCE_FINALIZED"
    monkeypatch.setattr(finality,"_persist_unknown_native_finality_locked",
                        persist)
    result=asyncio.run(
        finality.reconcile_unknown_original_native_signed_sources_once())
    assert result["finalized_without_resend"]==1
    assert result["new_signatures_created"]==0
    assert result["new_transactions_sent"]==0
    assert result["source_resubmission_permitted"] is False
    assert observed==[
        "owner",("finalized_source","SOL",tx),("persist","SENDING",tx),
    ]


@pytest.mark.parametrize("state",["PREPARED","SUBMITTED","SOURCE_FINALIZED"])
def test_only_uncertain_signed_states_may_promote(state):
    row=original("BTC")
    tx=row["source_tx_hash"]
    row.update(status=state,source_tx_hash=None,source_signed_tx_hash=tx)
    provisional={**row,"status":"SUBMITTED","source_tx_hash":tx}
    verified=finality._confirmed_original_source(
        provisional,proof("BTC"),WALLET["BTC"])
    cursor=Cursor([])
    with pytest.raises(HTTPException):
        finality._persist_unknown_native_finality_locked(
            cursor,original=row,verified=verified)
    assert cursor.queries==[]


def test_native_source_receipts_progress_across_owners_without_serial_rpc_wait(monkeypatch):
    other="did:privy:another_native_owner"
    jobs=[original("SOL"),original("XRP"),
          {**original("SOL"),"privy_did":other}]
    monkeypatch.setattr(finality,"_pending_original_source_submissions",
                        lambda limit=32:jobs)
    started=[]
    event=asyncio.Event()
    async def user(did):
        return {"id":did}
    async def receipt(did,*,asset,transaction_hash):
        started.append((did,asset))
        if len(started)>=2:
            event.set()
        await asyncio.wait_for(event.wait(),timeout=1)
        assert transaction_hash==TX[asset]
        return proof(asset)
    monkeypatch.setattr(finality,"fetch_privy_user",user)
    monkeypatch.setattr(finality,"_exact_owner_wallets",
                        lambda u,d,a:(WALLET[a],"0x"+"a"*40))
    monkeypatch.setattr(finality,"get_owner_native_sender_receipt",receipt)
    persisted=[]
    monkeypatch.setattr(finality,"_persist_independent_source_evidence",
                        lambda row,verified,**kw:
                          persisted.append((row["privy_did"],row["asset"])) or
                          "SOURCE_FINALIZED")
    result=asyncio.run(finality.reconcile_original_native_source_once())
    assert result["finalized_sources"]==3
    assert result["unverified_or_unavailable"]==0
    assert result["new_transactions_sent"]==0
    # Another owner moves ahead even if the first owner has an RPC wait.
    # The first owner's two orders remain in original journal order.
    assert started==[(DID,"SOL"),(other,"SOL"),(DID,"XRP")]
    assert len(persisted)==3


def test_original_eth_buy_and_unknown_sources_use_distinct_keyset_cursors(monkeypatch):
    from datetime import datetime,timezone
    base=datetime(2026,11,21,tzinfo=timezone.utc)
    row=original("SOL")|{
        "created_at":base,"intent_key":"native_"+"a"*64,
    }
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,query,args):
            self.query=query
            self.args=args
            assert "LIMIT %s" in query
            assert "OFFSET" not in query
        def fetchall(self):
            return [row] if len(self.args)==1 else []
    monkeypatch.setattr(finality,"_db_url",lambda:"postgres://test.invalid")
    monkeypatch.setattr(finality,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:DB())
    monkeypatch.setitem(finality._RECEIPT_PAGE_CURSORS,"buy",None)
    monkeypatch.setitem(finality._RECEIPT_PAGE_CURSORS,"unknown",None)
    assert finality._pending_original_eth_buy_submissions(1)==[row]
    assert finality._pending_unknown_signed_sources(1)==[row]
    assert finality._RECEIPT_PAGE_CURSORS["buy"]==(
        base,row["privy_did"],row["asset"],row["intent_key"])
    assert finality._RECEIPT_PAGE_CURSORS["unknown"]==(
        base,row["privy_did"],row["asset"],row["intent_key"])
