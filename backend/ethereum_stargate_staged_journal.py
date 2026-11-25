"""Durable two-leg AVAX execution journal.

One original owner order, one unresolved step, exact source and destination
credits. Each stage uses the existing central grant. There is no public
submission route. Intermediate stablecoins are never counted as ETH cash.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
import uuid
from decimal import Decimal,localcontext
from datetime import datetime
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from .storage import _db_url,_ensure_schema,_safe_did
from .ethereum_stargate_staged_policy import STARGATE_STAGED_POLICY,_route,inspect_staged_bridge_transaction
from .ethereum_stargate_staged_execution import staged_swap_transaction,staged_swap_route,_call_data
from .ethereum_settlement_plan import settlement_route,CROSS_COIN_CONTRACTS
from .ethereum_settlement_calldata import route_digest
from .shared_evm_central_runtime import _locked_original_evm_coin

_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ACTIVE=("SENDING","SUBMITTED","SOURCE_FINALIZED","UNCERTAIN")


def stage_kind(side,stage):
    if side not in ("BUY","SELL") or stage not in (1,2):
        raise ValueError("Invalid original AVAX stage")
    return "BRIDGE" if (side=="BUY")== (stage==1) else "SWAP"


def exact_stage_amount(order):
    scope=order["scope_json"]
    stage=order["stage"];side=order["side"]
    if stage==1:
        return int(order["original_input_atomic"])
    if stage!=2 or int(order["remaining_atomic"])<=0:
        raise ValueError("Original stage credit missing")
    # A profitable SELL can exceed the original buy cap. Complete the
    # entire actual credit in bounded chunks instead of leaving proceeds
    # stranded or silently increasing the remote user's permission.
    return min(int(order["remaining_atomic"]),int(scope["buy_source_atomic_cap"]))


def check_stage_prepared(order,prepared,now):
    scope=order["scope_json"]
    side=order["side"];stable=order["stable"];wallet=order["wallet_address"]
    amount=exact_stage_amount(order)
    if (type(prepared) is not dict or prepared.get("side")!=side
            or prepared.get("stable")!=stable or prepared.get("asset")!="AVAX"
            or prepared.get("wallet_address")!=wallet or prepared.get("input_atomic")!=str(amount)
            or type(prepared.get("issued_at")) is not int or not 0<=now-prepared["issued_at"]<30
            or int(prepared["source_balance_atomic"])<amount):
        raise ValueError("Staged quote differs from original owner order")
    minimum=int(prepared["minimum_output_atomic"])
    kind=stage_kind(side,order["stage"])
    if kind=="BRIDGE":
        src,dst,spender,token=_route(side,stable)
        if prepared["mode"]!="AVAX_DIRECT_STARGATE_TAXI_PREPARATION":
            raise ValueError("Expected original direct taxi quote")
        inspect_staged_bridge_transaction(prepared["transaction"],side=side,stable=stable,
            wallet=wallet,scope=scope,amount_atomic=amount,min_out_atomic=minimum,
            native_fee_atomic=int(prepared["native_bridge_fee_atomic"]))
    else:
        src=dst=43114
        token,target,spender,_=staged_swap_route(side,stable)
        if (prepared["mode"]!="AVAX_STAGED_SWAP_PREPARATION"
                or amount>int(scope["buy_source_atomic_cap" if side=="BUY" else "sell_source_atomic_cap"])
                or prepared["destination_token"]!=target
                or prepared["transaction"]!=staged_swap_transaction(side=side,stable=stable,
                    wallet=wallet,amount_atomic=amount,min_out_atomic=minimum,fee=prepared["fee_tier"])):
            raise ValueError("Expected original AVAX swap quote")
    if (prepared["source_chain_id"]!=src or prepared["destination_chain_id"]!=dst
            or prepared["source_token"]!=token):
        raise ValueError("Original staged chain or token differs")
    allowance=int(prepared["source_allowance_atomic"])
    approval=prepared.get("approval")
    if allowance<amount:
        reset=stable=="USDT" and allowance>0 and (kind=="BRIDGE" or side=="BUY")
        expected={"from":wallet,"to":token.lower(),"chainId":src,"value":"0",
            "data":_call_data("approve(address,uint256)",["address","uint256"],
                              [spender,0 if reset else amount])}
        if approval!=expected:
            raise ValueError("Original finite allowance differs")
        return "APPROVAL",expected
    if approval is not None:
        raise ValueError("Unnecessary original allowance transaction")
    return kind,prepared["transaction"]


def _owner_lock(cur,did):
    cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(did,))
    if not cur.fetchone():
        raise HTTPException(409,"Original staged owner unavailable")


def _current(cur,did,record,order=None):
    current=_locked_original_evm_coin(cur,did=did,asset="AVAX",
                                     stable=record["stable"],revision=record["budget_revision"])
    if (current["scope"].get("settlement_policy")!=STARGATE_STAGED_POLICY
            or any(current[k]!=record[k] for k in ("wallet_id","wallet_address","policy_sha256",
                                                    "policy_id","signer_id","scope"))):
        raise HTTPException(409,"Original AVAX remote policy changed")
    cur.execute("""SELECT status,stable,budget_revision,wallet_address,started_by_owner_at
        FROM mininex_live_evm_auto_sessions WHERE privy_did=%s AND asset='AVAX' FOR UPDATE""",(did,))
    session=cur.fetchone()
    if (not session or session["status"] not in ("ACTIVE","EXIT_PENDING")
            or session["stable"]!=record["stable"] or session["budget_revision"]!=record["budget_revision"]
            or session["wallet_address"]!=record["wallet_address"]):
        raise HTTPException(409,"Original AVAX session not active")
    if order and (order["scope_json"]!=current["scope"] or order["wallet_id"]!=current["wallet_id"]
                  or order["wallet_address"]!=current["wallet_address"]
                  or order["budget_revision"]!=current["budget_revision"]):
        raise HTTPException(409,"Original staged order consent changed")
    return session


def _lock_chain(cur,did,chain):
    # Account lock serializes claims. Keep all already-claimed network
    # transactions visible, including older native and direct bridge paths.
    for table in ("mininex_live_evm_router_approval_sends","mininex_live_evm_auto_broadcasts",
                  "mininex_evm_auto_fee_sends","mininex_live_evm_native_auto_steps",
                  "mininex_live_evm_native_sell_intents"):
        cur.execute(f"""SELECT 1 FROM {table} WHERE privy_did=%s AND chain_id=%s
            AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,chain))
        if cur.fetchone():
            raise HTTPException(409,"Earlier owner network send unresolved")
    cur.execute("""SELECT 1 FROM mininex_avax_staged_steps
        WHERE privy_did=%s AND chain_id=%s AND state IN ('SENDING','SUBMITTED','UNCERTAIN')
        UNION ALL SELECT 1 FROM mininex_ethereum_settlement_submissions s
        WHERE s.privy_did=%s AND s.source_chain_id=%s
          AND s.state IN ('SENDING','RECONCILIATION_REQUIRED','SUBMITTED')
          AND NOT EXISTS(SELECT 1 FROM mininex_ethereum_settlement_finality f WHERE f.quote_id=s.quote_id)
        LIMIT 1""",(did,chain,did,chain))
    if cur.fetchone():
        raise HTTPException(409,"Earlier owner bridge send unresolved")
    if chain==1:
        for table in ("mininex_live_eth_auto_broadcasts","mininex_live_eth_auto_native_steps"):
            cur.execute(f"""SELECT 1 FROM {table} WHERE privy_did=%s
                AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,))
            if cur.fetchone():
                raise HTTPException(409,"Earlier Ethereum send unresolved")
        cur.execute("""SELECT 1 FROM mininex_live_native_order_intents WHERE privy_did=%s
            AND chain='ethereum-mainnet' AND status IN
            ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,))
        if cur.fetchone():
            raise HTTPException(409,"Earlier native Ethereum send unresolved")


def create_order(did,record,*,side,prepared,budget_usd,position_usd,stable_usd,cost_usd):
    from .ethereum_settlement_journal import pending_bridge_order
    from .ethereum_settlement_funding import assert_ethereum_funds_locked
    from .ethereum_settlement_auto_plan import strategy_bridge_position_locked
    did=_safe_did(did);db=_db_url();_ensure_schema(db)
    order_id=uuid.uuid4()
    amount=int(prepared["input_atomic"])
    order={"order_id":order_id,"privy_did":did,"side":side,"stable":record["stable"],
           "wallet_id":record["wallet_id"],"wallet_address":record["wallet_address"],
           "scope_json":record["scope"],"budget_revision":record["budget_revision"],
           "stage":1,"original_input_atomic":amount,"remaining_atomic":0}
    check_stage_prepared(order,prepared,int(time.time()))
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _owner_lock(cur,did)
            session=_current(cur,did,record)
            if side=="BUY" and session["status"]!="ACTIVE":
                raise HTTPException(409,"No new BUY after Stop or Exit")
            if pending_bridge_order(cur,did,"AVAX"):
                raise HTTPException(409,"Original AVAX order already pending")
            cur.execute("""SELECT 1 FROM mininex_live_evm_budget_reservations
                WHERE privy_did=%s AND asset='AVAX' AND state IN
                ('RESERVED','SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                UNION ALL SELECT 1 FROM mininex_live_evm_native_sell_intents
                WHERE privy_did=%s AND asset='AVAX' AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                UNION ALL SELECT 1 FROM mininex_live_evm_native_auto_steps
                WHERE privy_did=%s AND asset='AVAX' AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,did,did))
            if cur.fetchone():
                raise HTTPException(409,"Earlier AVAX local order unresolved")
            if side=="BUY":
                pending=assert_ethereum_funds_locked(cur,did=did,asset="AVAX",stable=order["stable"],
                    input_atomic=amount,balance_atomic=int(prepared["source_balance_atomic"]),stable_usd=stable_usd)
                with localcontext() as ctx:
                    ctx.prec=100
                    if (Decimal(position_usd)+Decimal(cost_usd)+Decimal(pending["pending_coin_usd"])
                        +Decimal(amount).scaleb(-6)*Decimal(stable_usd)>Decimal(budget_usd)):
                        raise HTTPException(409,"Original AVAX capital including first-stage costs allocated")
            elif strategy_bridge_position_locked(cur,did,"AVAX",order["wallet_address"])<amount:
                raise HTTPException(409,"Original AVAX strategy position changed")
            cur.execute("""INSERT INTO mininex_avax_staged_orders
                (order_id,privy_did,side,stable,wallet_address,wallet_id,budget_revision,
                 scope_json,started_by_owner_at,original_input_atomic)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (order_id,did,side,order["stable"],order["wallet_address"],order["wallet_id"],
                 order["budget_revision"],Jsonb(order["scope_json"]),session["started_by_owner_at"],amount))
            return cur.fetchone()


def read_open_order(did):
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM mininex_avax_staged_orders WHERE privy_did=%s AND state='OPEN'",(_safe_did(did),))
            return cur.fetchone()


def read_pending_step(order_id):
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT * FROM mininex_avax_staged_steps WHERE order_id=%s
                AND state IN ('SENDING','SUBMITTED','SOURCE_FINALIZED','UNCERTAIN')""",(order_id,))
            return cur.fetchone()


def claim_step(did,order_id,record,prepared):
    did=_safe_did(did);db=_db_url();_ensure_schema(db)
    step_id=uuid.uuid4();key="avax_"+step_id.hex
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _owner_lock(cur,did)
            cur.execute("SELECT * FROM mininex_avax_staged_orders WHERE order_id=%s AND privy_did=%s FOR UPDATE",(order_id,did))
            order=cur.fetchone()
            if not order or order["state"]!="OPEN":
                raise HTTPException(409,"Original AVAX order not pending")
            session=_current(cur,did,record,order)
            if order["side"]=="BUY" and order["stage"]==1 and session["status"]=="EXIT_PENDING":
                cur.execute("""SELECT 1 FROM mininex_avax_staged_steps WHERE order_id=%s
                    AND state IN ('SENDING','SUBMITTED','SOURCE_FINALIZED','UNCERTAIN') LIMIT 1""",(order_id,))
                if cur.fetchone():
                    raise HTTPException(409,"Prior AVAX step unresolved")
                cur.execute("UPDATE mininex_avax_staged_orders SET state='CANCELLED',updated_at=now() WHERE order_id=%s",(order_id,))
                return None
            kind,tx=check_stage_prepared(order,prepared,int(time.time()))
            _lock_chain(cur,did,tx["chainId"])
            url="https://api.privy.io/v1/wallets/"+record["wallet_id"]+"/rpc"
            body={"method":"eth_sendTransaction","caip2":"eip155:"+str(tx["chainId"]),
                "params":{"transaction":{"to":tx["to"],"data":tx["data"],"value":tx["value"]}}}
            digest=hashlib.sha256(json.dumps({"url":url,"body":body,"key":key},sort_keys=True,separators=(",",":")).encode()).hexdigest()
            cur.execute("""INSERT INTO mininex_avax_staged_steps
                (step_id,order_id,privy_did,stage,kind,chain_id,prepared,request_sha256,state,scan_block)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'SENDING',%s) RETURNING step_id""",
                (step_id,order_id,did,order["stage"],kind,tx["chainId"],Jsonb(prepared),digest,
                 prepared.get("destination_start_block") if kind=="BRIDGE" else None))
            if not cur.fetchone():
                raise HTTPException(409,"Original staged step not claimed")
    return {"step_id":step_id,"key":key,"digest":digest,"url":url,"body":body,"kind":kind}


def finish_submission(claim,tx_hash):
    if tx_hash is not None and (type(tx_hash) is not str or not _HASH.fullmatch(tx_hash.lower())):
        raise ValueError("Invalid claimed AVAX transaction hash")
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""UPDATE mininex_avax_staged_steps SET state=%s,tx_hash=%s,updated_at=now()
                WHERE step_id=%s AND request_sha256=%s AND state='SENDING' RETURNING step_id""",
                ("SUBMITTED" if tx_hash else "UNCERTAIN",tx_hash.lower() if tx_hash else None,claim["step_id"],claim["digest"]))
            if not cur.fetchone():
                raise HTTPException(409,"Original AVAX submission outcome needs reconciliation")


def pending_reconcile_steps():
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT * FROM mininex_avax_staged_steps
                WHERE state IN ('SUBMITTED','SOURCE_FINALIZED') ORDER BY updated_at LIMIT 50""")
            return list(cur.fetchall())


def _complete_parent_locked(cur,order):
    """Expose one original settled order to the existing fill/exit/funding ledger.

    The separate step journal retains every approval, exact actual bridge
    fee, receipt, GUID and chain-specific gas for subsequent net accounting.
    Never create a parent before both stages settle.
    """
    cur.execute("""SELECT * FROM mininex_avax_staged_steps WHERE order_id=%s
        AND kind IN ('SWAP','BRIDGE') AND state='FINALIZED'
        ORDER BY stage,created_at,step_id""",(order["order_id"],))
    steps=cur.fetchall()
    if not steps or steps[0]["stage"]!=1 or steps[-1]["stage"]!=2:
        raise ValueError("Original two-leg AVAX fills missing")
    first,last=steps[0],steps[-1]
    bridge=next(s for s in steps if s["kind"]=="BRIDGE")
    route=settlement_route("AVAX",order["side"],order["stable"])
    tx=first["prepared"]["transaction"]
    final_hash=last["proof"].get("destination_hash") or last["tx_hash"]
    if (not _HASH.fullmatch(final_hash) or not _HASH.fullmatch(first["tx_hash"])
            or not 0<int(order["final_minimum_atomic"])<=int(order["final_received_atomic"])):
        raise ValueError("Original staged parent output unverified")
    issued=first["prepared"]["issued_at"]
    source=route["settlement_contract"] if order["side"]=="BUY" else CROSS_COIN_CONTRACTS["AVAX"]
    target=CROSS_COIN_CONTRACTS["AVAX"] if order["side"]=="BUY" else route["settlement_contract"]
    digest=route_digest(tx,route["source_chain_id"])
    cur.execute("""INSERT INTO mininex_ethereum_settlement_quotes
        (quote_id,privy_did,wallet_address,asset,side,stable,source_chain_id,destination_chain_id,
         source_token,destination_token,input_atomic,minimum_output_atomic,issued_at,expires_at,
         route_sha256,unsigned_transaction)
        VALUES (%s,%s,%s,'AVAX',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (order["order_id"],order["privy_did"],order["wallet_address"],order["side"],order["stable"],
         route["source_chain_id"],route["destination_chain_id"],source,target,order["original_input_atomic"],
         order["final_minimum_atomic"],issued,issued+30,digest,Jsonb(tx)))
    cur.execute("""INSERT INTO mininex_ethereum_settlement_submissions
        (quote_id,privy_did,intent_key,source_chain_id,request_sha256,state,source_tx_hash)
        VALUES (%s,%s,%s,%s,%s,'SUBMITTED',%s)""",
        (order["order_id"],order["privy_did"],"avax_"+order["order_id"].hex,route["source_chain_id"],
         first["request_sha256"],first["tx_hash"]))
    # The parent source gas is only in the source chain's native units.
    # Never sum ETH and AVAX gas atomics into a fictitious single fee.
    source_gas=sum(int(s["proof"]["source_gas_atomic"]) for s in steps if s["chain_id"]==route["source_chain_id"])
    cur.execute("""INSERT INTO mininex_ethereum_settlement_finality
        (quote_id,privy_did,source_chain_id,destination_chain_id,source_tx_hash,destination_tx_hash,
         bridge_deposit_id,received_atomic,source_gas_atomic,outcome)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'FINALIZED')""",
        (order["order_id"],order["privy_did"],route["source_chain_id"],route["destination_chain_id"],
         first["tx_hash"],final_hash,int(bridge["proof"]["guid"],16),order["final_received_atomic"],source_gas))


def record_step_proof(step,proof,scan_block=None):
    """Persist only internal independently verified results, with compare-and-set."""
    did=step["privy_did"]
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _owner_lock(cur,did)
            cur.execute("SELECT * FROM mininex_avax_staged_orders WHERE order_id=%s FOR UPDATE",(step["order_id"],))
            order=cur.fetchone()
            cur.execute("SELECT * FROM mininex_avax_staged_steps WHERE step_id=%s FOR UPDATE",(step["step_id"],))
            current=cur.fetchone()
            if (not order or order["state"]!="OPEN" or not current or current["state"] not in ("SUBMITTED","SOURCE_FINALIZED")
                    or current["tx_hash"]!=step["tx_hash"] or order["stage"]!=current["stage"]):
                return False
            status=proof["state"]
            if status in ("SOURCE_PENDING","DESTINATION_PENDING"):
                if status=="DESTINATION_PENDING" and current["kind"]=="BRIDGE":
                    cur.execute("""UPDATE mininex_avax_staged_steps SET state='SOURCE_FINALIZED',
                        proof=%s,scan_block=COALESCE(%s,scan_block),updated_at=now() WHERE step_id=%s""",
                        (Jsonb(proof),scan_block,step["step_id"]))
                else:
                    cur.execute("UPDATE mininex_avax_staged_steps SET updated_at=now() WHERE step_id=%s",(step["step_id"],))
                return True
            if status=="SOURCE_REVERTED":
                cur.execute("UPDATE mininex_avax_staged_steps SET state='REVERTED',proof=%s,updated_at=now() WHERE step_id=%s",
                            (Jsonb(proof),step["step_id"]))
                if order["stage"]==1 and current["kind"]!="APPROVAL":
                    cur.execute("UPDATE mininex_avax_staged_orders SET state='REVERTED',updated_at=now() WHERE order_id=%s",(order["order_id"],))
                return True
            expected={"APPROVAL":"APPROVAL_FINALIZED","BRIDGE":"BRIDGE_CREDIT_VERIFIED","SWAP":"SWAP_CREDIT_VERIFIED"}[current["kind"]]
            if status!=expected:
                raise ValueError("Unexpected AVAX stage receipt outcome")
            cur.execute("UPDATE mininex_avax_staged_steps SET state='FINALIZED',proof=%s,updated_at=now() WHERE step_id=%s",
                        (Jsonb(proof),step["step_id"]))
            if current["kind"]=="APPROVAL":
                return True
            if proof.get("destination_credit_verified") is not True:
                raise ValueError("Missing actual staged credit")
            received=int(proof["received_atomic"])
            minimum=int(current["prepared"]["minimum_output_atomic"])
            if received<minimum or minimum<=0:
                raise ValueError("Staged credit below original exact quote")
            if order["stage"]==1:
                cur.execute("""UPDATE mininex_avax_staged_orders SET stage=2,remaining_atomic=%s,updated_at=now()
                    WHERE order_id=%s""",(received,order["order_id"]))
                return True
            spent=int(current["prepared"]["input_atomic"])
            if spent<=0 or spent>int(order["remaining_atomic"]):
                raise ValueError("Original remaining stage credit exceeded")
            cur.execute("""UPDATE mininex_avax_staged_orders SET remaining_atomic=remaining_atomic-%s,
                final_received_atomic=final_received_atomic+%s,final_minimum_atomic=final_minimum_atomic+%s,
                updated_at=now() WHERE order_id=%s RETURNING *""",(spent,received,minimum,order["order_id"]))
            updated=cur.fetchone()
            if int(updated["remaining_atomic"])==0:
                _complete_parent_locked(cur,updated)
                cur.execute("UPDATE mininex_avax_staged_orders SET state='FINALIZED',updated_at=now() WHERE order_id=%s",(order["order_id"],))
            return True


def cancel_unspent_buy_for_exit(did,order_id):
    """Only unspent initial BUY may cancel on Exit; preserve all spent orders."""
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _owner_lock(cur,did)
            cur.execute("""UPDATE mininex_avax_staged_orders o SET state='CANCELLED',updated_at=now()
                WHERE o.order_id=%s AND o.privy_did=%s AND o.side='BUY' AND o.stage=1 AND o.state='OPEN'
                  AND EXISTS(SELECT 1 FROM mininex_live_evm_auto_sessions s
                    WHERE s.privy_did=o.privy_did AND s.asset='AVAX' AND s.status='EXIT_PENDING')
                  AND NOT EXISTS(SELECT 1 FROM mininex_avax_staged_steps t WHERE t.order_id=o.order_id
                    AND (t.state IN ('SENDING','SUBMITTED','SOURCE_FINALIZED','UNCERTAIN')
                         OR (t.kind!='APPROVAL' AND t.state='FINALIZED')))
                RETURNING order_id""",(order_id,did))
            return cur.fetchone() is not None
