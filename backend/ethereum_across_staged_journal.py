"""Durable two-leg Across execution journal.

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
from datetime import datetime,timezone
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from .storage import _db_url,_ensure_schema,_safe_did
from .ethereum_across_staged_plan import STAGED_ACROSS_POLICY,STAGED_ACROSS_ASSETS,staged_route,inspect_staged_bridge,staged_local_transaction
from .ethereum_stargate_staged_execution import _call_data
from .ethereum_settlement_calldata import DIAMOND
from .native_evm_v3_quote import CHAINS
from .ethereum_settlement_plan import settlement_route,CROSS_COIN_CONTRACTS
from .ethereum_settlement_calldata import route_digest
from .shared_evm_central_runtime import _locked_original_evm_coin

_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ACTIVE=("SENDING","SUBMITTED","SOURCE_FINALIZED","UNCERTAIN")


def staged_order_identity(did,asset,started,signal_time):
    """One original Across market event per owner session, across requotes/restarts."""
    if not isinstance(started,datetime) or started.tzinfo is None or type(signal_time) is not int or signal_time<=0:
        raise ValueError("Original Across session and market event required")
    if asset not in STAGED_ACROSS_ASSETS:
        raise ValueError("Unsupported staged asset")
    identity=json.dumps([_safe_did(did),asset,started.astimezone(timezone.utc).isoformat(),
                         signal_time//60],separators=(",",":"))
    return uuid.uuid5(uuid.NAMESPACE_URL,"mininex:across:"+identity)


def stage_kind(side,stage):
    if side not in ("BUY","SELL") or stage not in (1,2):
        raise ValueError("Invalid original Across stage")
    return "BRIDGE" if (side=="BUY")== (stage==1) else "SWAP"


def exact_stage_amount(order):
    scope=order["scope_json"]
    stage=order["stage"];side=order["side"]
    if stage==1:
        amount=int(order["original_input_atomic"])
        cap=int(scope["buy_source_atomic_cap" if side=="BUY" else "sell_source_atomic_cap"])
    elif stage==2:
        amount=int(order["remaining_atomic"])
        cap=int(scope["transit_atomic_cap"])
    else:
        raise ValueError("Invalid original stage")
    if not 0<amount<=cap:
        raise ValueError("Original stage exceeds signed token scope")
    return amount


def check_stage_prepared(order,prepared,now):
    asset,side,stable,wallet=(order[k] for k in ("asset","side","stable","wallet_address"))
    scope=order["scope_json"]
    if (scope.get("asset")!=asset or scope.get("stable")!=stable
            or scope.get("settlement_policy")!=STAGED_ACROSS_POLICY):
        raise ValueError("Original staged owner permission unavailable")
    route=staged_route(asset,side,stable)
    amount=exact_stage_amount(order)
    kind=stage_kind(side,order["stage"])
    if kind=="BRIDGE":
        src,dst=route["bridge_source"],route["bridge_target"]
        terms=prepared["terms"];preflight=prepared["preflight"]
        tx=terms["unsigned_transaction"]
        review=inspect_staged_bridge(terms,tx,now=now)
        if (prepared.get("mode")!="ACROSS_STAGED_BRIDGE_PREPARATION"
                or review["route_sha256"]!=terms["route_sha256"]
                or preflight.get("route_sha256")!=terms["route_sha256"]
                or preflight.get("quote_id")!=terms["quote_id"]
                or type(prepared.get("destination_start_block")) is not int
                or prepared["destination_start_block"]<=0):
            raise ValueError("Original bridge preparation changed")
        approval=preflight.get("manual_approval")
        balance=int(preflight["verified_source_balance_atomic"])
        allowance=int(preflight["verified_source_allowance_atomic"]) if approval else amount
        if approval:
            approval={**approval,"chainId":src[0],"value":str(int(approval["value"],16))}
        evidence=terms
        spender=DIAMOND
        reset=(stable if side=="BUY" else route["transit_symbol"])=="USDT" and allowance>0
    else:
        src,dst=route["swap_source"],route["swap_target"]
        evidence=prepared
        if prepared.get("mode")!="ACROSS_STAGED_SWAP_PREPARATION":
            raise ValueError("Original swap preparation changed")
        tx=staged_local_transaction(asset,side,stable,wallet,amount,
            int(prepared["minimum_output_atomic"]),prepared["fee_tier"])
        if prepared["transaction"]!=tx:
            raise ValueError("Original swap calldata changed")
        balance=int(prepared["source_balance_atomic"])
        allowance=int(prepared["source_allowance_atomic"])
        approval=prepared.get("approval")
        spender=CHAINS[asset].router.lower()
        reset=side=="BUY" and route["transit_symbol"]=="USDT" and allowance>0
    if (evidence.get("asset")!=asset or evidence.get("side")!=side or evidence.get("stable")!=stable
            or evidence.get("wallet_address")!=wallet or evidence.get("input_atomic")!=str(amount)
            or evidence.get("source_chain_id")!=src[0] or evidence.get("destination_chain_id")!=dst[0]
            or evidence.get("source_token")!=src[1] or evidence.get("destination_token")!=dst[1]
            or type(evidence.get("issued_at")) is not int or not 0<=now-evidence["issued_at"]<30
            or balance<amount or allowance<0 or int(evidence["minimum_output_atomic"])<=0):
        raise ValueError("Staged quote differs from original order")
    if allowance<amount:
        expected={"from":wallet,"to":src[1],"chainId":src[0],"value":"0",
                  "data":_call_data("approve(address,uint256)",["address","uint256"],
                                    [spender,0 if reset else amount])}
        if approval!=expected:
            raise ValueError("Original finite allowance differs")
        return "APPROVAL",expected
    if approval is not None:
        raise ValueError("Unnecessary allowance transaction")
    return kind,tx


def stage_evidence(prepared):
    return prepared["terms"] if prepared.get("mode")=="ACROSS_STAGED_BRIDGE_PREPARATION" else prepared


def stage_source_balance(prepared):
    return int(prepared["preflight"]["verified_source_balance_atomic"] if
        prepared.get("mode")=="ACROSS_STAGED_BRIDGE_PREPARATION" else prepared["source_balance_atomic"])


def _owner_lock(cur,did):
    cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(did,))
    if not cur.fetchone():
        raise HTTPException(409,"Original staged owner unavailable")


def _current(cur,did,record,order=None):
    current=_locked_original_evm_coin(cur,did=did,asset=record["asset"],
                                     stable=record["stable"],revision=record["budget_revision"])
    if (current["scope"].get("settlement_policy")!=STAGED_ACROSS_POLICY
            or any(current[k]!=record[k] for k in ("wallet_id","wallet_address","policy_sha256",
                                                    "policy_id","signer_id","scope"))):
        raise HTTPException(409,"Original Across remote policy changed")
    cur.execute("""SELECT status,stable,budget_revision,wallet_address,started_by_owner_at
        FROM mininex_live_evm_auto_sessions WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,record["asset"]))
    session=cur.fetchone()
    if (not session or session["status"] not in ("ACTIVE","EXIT_PENDING")
            or session["stable"]!=record["stable"] or session["budget_revision"]!=record["budget_revision"]
            or session["wallet_address"]!=record["wallet_address"]):
        raise HTTPException(409,"Original Across session not active")
    if order and (order["asset"]!=record["asset"] or order["scope_json"]!=current["scope"] or order["wallet_id"]!=current["wallet_id"]
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
    cur.execute("""SELECT 1 FROM mininex_across_staged_steps
        WHERE privy_did=%s AND chain_id=%s AND state IN ('SENDING','SUBMITTED','UNCERTAIN')
        UNION ALL SELECT 1 FROM mininex_ethereum_settlement_submissions s
        WHERE s.privy_did=%s AND s.source_chain_id=%s
          AND s.state IN ('SENDING','RECONCILIATION_REQUIRED','SUBMITTED')
          AND NOT EXISTS(SELECT 1 FROM mininex_ethereum_settlement_finality f WHERE f.quote_id=s.quote_id)
        UNION ALL SELECT 1 FROM mininex_avax_staged_steps
        WHERE privy_did=%s AND chain_id=%s AND state IN ('SENDING','SUBMITTED','UNCERTAIN')
        LIMIT 1""",(did,chain,did,chain,did,chain))
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


def create_order(did,record,*,side,prepared,budget_usd,position_usd,stable_usd,cost_usd,
                 session_started_at,signal_time):
    from .ethereum_settlement_journal import pending_bridge_order
    from .ethereum_settlement_funding import assert_ethereum_funds_locked
    from .ethereum_settlement_auto_plan import strategy_bridge_position_locked
    did=_safe_did(did);db=_db_url();_ensure_schema(db)
    asset=record["asset"]
    order_id=staged_order_identity(did,asset,session_started_at,signal_time)
    amount=int(stage_evidence(prepared)["input_atomic"])
    order={"order_id":order_id,"asset":asset,"privy_did":did,"side":side,"stable":record["stable"],
           "wallet_id":record["wallet_id"],"wallet_address":record["wallet_address"],
           "scope_json":record["scope"],"budget_revision":record["budget_revision"],
           "stage":1,"original_input_atomic":amount,"remaining_atomic":0}
    check_stage_prepared(order,prepared,int(time.time()))
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _owner_lock(cur,did)
            session=_current(cur,did,record)
            if session["started_by_owner_at"]!=session_started_at:
                raise HTTPException(409,"Original Across signal belongs to a replaced session")
            if side=="BUY" and session["status"]!="ACTIVE":
                raise HTTPException(409,"No new BUY after Stop or Exit")
            if pending_bridge_order(cur,did,asset):
                raise HTTPException(409,"Original Across order already pending")
            cur.execute("""SELECT 1 FROM mininex_live_evm_budget_reservations
                WHERE privy_did=%s AND asset=%s AND state IN
                ('RESERVED','SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                UNION ALL SELECT 1 FROM mininex_live_evm_native_sell_intents
                WHERE privy_did=%s AND asset=%s AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                UNION ALL SELECT 1 FROM mininex_live_evm_native_auto_steps
                WHERE privy_did=%s AND asset=%s AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,asset,did,asset,did,asset))
            if cur.fetchone():
                raise HTTPException(409,"Earlier Across local order unresolved")
            if side=="BUY":
                pending=assert_ethereum_funds_locked(cur,did=did,asset=asset,stable=order["stable"],
                    input_atomic=amount,balance_atomic=stage_source_balance(prepared),stable_usd=stable_usd)
                with localcontext() as ctx:
                    ctx.prec=100
                    if (Decimal(position_usd)+Decimal(cost_usd)+Decimal(pending["pending_coin_usd"])
                        +Decimal(amount).scaleb(-6)*Decimal(stable_usd)>Decimal(budget_usd)):
                        raise HTTPException(409,"Original Across capital including first-stage costs allocated")
            elif strategy_bridge_position_locked(cur,did,asset,order["wallet_address"])<amount:
                raise HTTPException(409,"Original Across strategy position changed")
            cur.execute("""INSERT INTO mininex_across_staged_orders
                (order_id,asset,privy_did,side,stable,wallet_address,wallet_id,budget_revision,
                 scope_json,started_by_owner_at,original_input_atomic)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (order_id) DO NOTHING RETURNING *""",
                (order_id,asset,did,side,order["stable"],order["wallet_address"],order["wallet_id"],
                 order["budget_revision"],Jsonb(order["scope_json"]),session["started_by_owner_at"],amount))
            saved=cur.fetchone()
            if saved is None:
                raise HTTPException(409,"Original Across signal already processed")
            return saved


def read_open_order(did,asset):
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM mininex_across_staged_orders WHERE privy_did=%s AND asset=%s AND state='OPEN'",(_safe_did(did),asset))
            return cur.fetchone()


def read_pending_step(order_id):
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT * FROM mininex_across_staged_steps WHERE order_id=%s
                AND state IN ('SENDING','SUBMITTED','SOURCE_FINALIZED','UNCERTAIN')""",(order_id,))
            return cur.fetchone()


def claim_step(did,order_id,record,prepared,*,expected_updated_at):
    did=_safe_did(did);db=_db_url();_ensure_schema(db)
    step_id=uuid.uuid4();key="across_"+step_id.hex
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _owner_lock(cur,did)
            cur.execute("SELECT * FROM mininex_across_staged_orders WHERE order_id=%s AND privy_did=%s FOR UPDATE",(order_id,did))
            order=cur.fetchone()
            if not order or order["state"]!="OPEN":
                raise HTTPException(409,"Original Across order not pending")
            if order["updated_at"]!=expected_updated_at:
                raise HTTPException(409,"Original Across step snapshot changed; refresh quote")
            session=_current(cur,did,record,order)
            cur.execute("""SELECT 1 FROM mininex_across_staged_steps WHERE order_id=%s
                AND state IN ('SENDING','SUBMITTED','SOURCE_FINALIZED','UNCERTAIN') LIMIT 1""",(order_id,))
            if cur.fetchone():
                raise HTTPException(409,"Prior Across step unresolved")
            if order["side"]=="BUY" and order["stage"]==1 and session["status"]=="EXIT_PENDING":
                cur.execute("UPDATE mininex_across_staged_orders SET state='CANCELLED',updated_at=now() WHERE order_id=%s",(order_id,))
                return None
            kind,tx=check_stage_prepared(order,prepared,int(time.time()))
            _lock_chain(cur,did,tx["chainId"])
            url="https://api.privy.io/v1/wallets/"+record["wallet_id"]+"/rpc"
            body={"method":"eth_sendTransaction","caip2":"eip155:"+str(tx["chainId"]),
                "params":{"transaction":{"to":tx["to"],"data":tx["data"],"value":tx["value"]}}}
            digest=hashlib.sha256(json.dumps({"url":url,"body":body,"key":key},sort_keys=True,separators=(",",":")).encode()).hexdigest()
            cur.execute("""INSERT INTO mininex_across_staged_steps
                (step_id,order_id,privy_did,stage,kind,chain_id,prepared,request_sha256,state,scan_block)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'SENDING',%s) RETURNING step_id""",
                (step_id,order_id,did,order["stage"],kind,tx["chainId"],Jsonb(prepared),digest,
                 prepared.get("destination_start_block") if kind=="BRIDGE" else None))
            if not cur.fetchone():
                raise HTTPException(409,"Original staged step not claimed")
            cur.execute("UPDATE mininex_across_staged_orders SET updated_at=now() WHERE order_id=%s",(order_id,))
    return {"step_id":step_id,"key":key,"digest":digest,"url":url,"body":body,"kind":kind}


def finish_submission(claim,tx_hash):
    if tx_hash is not None and (type(tx_hash) is not str or not _HASH.fullmatch(tx_hash.lower())):
        raise ValueError("Invalid claimed Across transaction hash")
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""UPDATE mininex_across_staged_steps SET state=%s,tx_hash=%s,updated_at=now()
                WHERE step_id=%s AND request_sha256=%s AND state='SENDING' RETURNING step_id""",
                ("SUBMITTED" if tx_hash else "UNCERTAIN",tx_hash.lower() if tx_hash else None,claim["step_id"],claim["digest"]))
            if not cur.fetchone():
                raise HTTPException(409,"Original Across submission outcome needs reconciliation")


def pending_reconcile_steps():
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT * FROM mininex_across_staged_steps
                WHERE state IN ('SUBMITTED','SOURCE_FINALIZED') ORDER BY updated_at LIMIT 50""")
            return list(cur.fetchall())


def _complete_parent_locked(cur,order):
    """Expose one original settled order to the existing fill/exit/funding ledger.

    The separate step journal retains every approval, exact actual bridge
    fee, receipt, GUID and chain-specific gas for subsequent net accounting.
    Never create a parent before both stages settle.
    """
    cur.execute("""SELECT * FROM mininex_across_staged_steps WHERE order_id=%s
        AND kind IN ('SWAP','BRIDGE') AND state='FINALIZED'
        ORDER BY stage,created_at,step_id""",(order["order_id"],))
    steps=cur.fetchall()
    if len(steps)!=2 or steps[0]["stage"]!=1 or steps[1]["stage"]!=2:
        raise ValueError("Original two-leg Across fills missing")
    first,last=steps[0],steps[-1]
    bridge=next(s for s in steps if s["kind"]=="BRIDGE")
    asset=order["asset"]
    route=settlement_route(asset,order["side"],order["stable"])
    original=stage_evidence(first["prepared"])
    tx=original.get("unsigned_transaction") or original["transaction"]
    final_hash=last["proof"].get("destination_hash") or last["tx_hash"]
    if (not _HASH.fullmatch(final_hash) or not _HASH.fullmatch(first["tx_hash"])
            or not 0<int(order["final_minimum_atomic"])<=int(order["final_received_atomic"])):
        raise ValueError("Original staged parent output unverified")
    issued=original["issued_at"]
    source=route["settlement_contract"] if order["side"]=="BUY" else CROSS_COIN_CONTRACTS[asset]
    target=CROSS_COIN_CONTRACTS[asset] if order["side"]=="BUY" else route["settlement_contract"]
    digest=route_digest(tx,route["source_chain_id"])
    cur.execute("""INSERT INTO mininex_ethereum_settlement_quotes
        (quote_id,privy_did,wallet_address,asset,side,stable,source_chain_id,destination_chain_id,
         source_token,destination_token,input_atomic,minimum_output_atomic,issued_at,expires_at,
         route_sha256,unsigned_transaction)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (order["order_id"],order["privy_did"],order["wallet_address"],asset,order["side"],order["stable"],
         route["source_chain_id"],route["destination_chain_id"],source,target,order["original_input_atomic"],
         order["final_minimum_atomic"],issued,issued+30,digest,Jsonb(tx)))
    cur.execute("""INSERT INTO mininex_ethereum_settlement_submissions
        (quote_id,privy_did,intent_key,source_chain_id,request_sha256,state,source_tx_hash)
        VALUES (%s,%s,%s,%s,%s,'SUBMITTED',%s)""",
        (order["order_id"],order["privy_did"],"across_"+order["order_id"].hex,route["source_chain_id"],
         first["request_sha256"],first["tx_hash"]))
    # The parent source gas is only in the source chain's native units.
    # Never sum ETH and Across gas atomics into a fictitious single fee.
    source_gas=sum(int(s["proof"]["source_gas_atomic"]) for s in steps if s["chain_id"]==route["source_chain_id"])
    cur.execute("""INSERT INTO mininex_ethereum_settlement_finality
        (quote_id,privy_did,source_chain_id,destination_chain_id,source_tx_hash,destination_tx_hash,
         bridge_deposit_id,received_atomic,source_gas_atomic,outcome)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'FINALIZED')""",
        (order["order_id"],order["privy_did"],route["source_chain_id"],route["destination_chain_id"],
         first["tx_hash"],final_hash,int(bridge["proof"]["deposit_id"]),order["final_received_atomic"],source_gas))


def record_step_proof(step,proof,scan_block=None):
    """Persist only internal independently verified results, with compare-and-set."""
    did=step["privy_did"]
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _owner_lock(cur,did)
            cur.execute("SELECT * FROM mininex_across_staged_orders WHERE order_id=%s FOR UPDATE",(step["order_id"],))
            order=cur.fetchone()
            cur.execute("SELECT * FROM mininex_across_staged_steps WHERE step_id=%s FOR UPDATE",(step["step_id"],))
            current=cur.fetchone()
            if (not order or order["state"]!="OPEN" or not current or current["state"] not in ("SUBMITTED","SOURCE_FINALIZED")
                    or current["tx_hash"]!=step["tx_hash"] or order["stage"]!=current["stage"]):
                return False
            status=proof["state"]
            if status in ("SOURCE_PENDING","DESTINATION_PENDING"):
                if status=="DESTINATION_PENDING" and current["kind"]=="BRIDGE":
                    cur.execute("""UPDATE mininex_across_staged_steps SET state='SOURCE_FINALIZED',
                        proof=%s,scan_block=COALESCE(%s,scan_block),updated_at=now() WHERE step_id=%s""",
                        (Jsonb(proof),scan_block,step["step_id"]))
                else:
                    cur.execute("UPDATE mininex_across_staged_steps SET updated_at=now() WHERE step_id=%s",(step["step_id"],))
                return True
            if status=="SOURCE_REVERTED":
                cur.execute("UPDATE mininex_across_staged_steps SET state='REVERTED',proof=%s,updated_at=now() WHERE step_id=%s",
                            (Jsonb(proof),step["step_id"]))
                if order["stage"]==1 and current["kind"]!="APPROVAL":
                    cur.execute("UPDATE mininex_across_staged_orders SET state='REVERTED',updated_at=now() WHERE order_id=%s",(order["order_id"],))
                return True
            expected={"APPROVAL":"APPROVAL_FINALIZED","BRIDGE":"BRIDGE_CREDIT_VERIFIED","SWAP":"SWAP_CREDIT_VERIFIED"}[current["kind"]]
            if status!=expected:
                raise ValueError("Unexpected Across stage receipt outcome")
            cur.execute("UPDATE mininex_across_staged_steps SET state='FINALIZED',proof=%s,updated_at=now() WHERE step_id=%s",
                        (Jsonb(proof),step["step_id"]))
            if current["kind"]=="APPROVAL":
                return True
            if proof.get("destination_credit_verified") is not True:
                raise ValueError("Missing actual staged credit")
            received=int(proof["received_atomic"])
            minimum=int(stage_evidence(current["prepared"])["minimum_output_atomic"])
            if received<minimum or minimum<=0:
                raise ValueError("Staged credit below original exact quote")
            if order["stage"]==1:
                cur.execute("""UPDATE mininex_across_staged_orders SET stage=2,remaining_atomic=%s,updated_at=now()
                    WHERE order_id=%s""",(received,order["order_id"]))
                return True
            spent=int(stage_evidence(current["prepared"])["input_atomic"])
            if spent<=0 or spent>int(order["remaining_atomic"]):
                raise ValueError("Original remaining stage credit exceeded")
            cur.execute("""UPDATE mininex_across_staged_orders SET remaining_atomic=remaining_atomic-%s,
                final_received_atomic=final_received_atomic+%s,final_minimum_atomic=final_minimum_atomic+%s,
                updated_at=now() WHERE order_id=%s RETURNING *""",(spent,received,minimum,order["order_id"]))
            updated=cur.fetchone()
            if int(updated["remaining_atomic"])==0:
                _complete_parent_locked(cur,updated)
                cur.execute("UPDATE mininex_across_staged_orders SET state='FINALIZED',updated_at=now() WHERE order_id=%s",(order["order_id"],))
            return True


def cancel_unspent_buy_for_exit(did,order_id):
    """Only unspent initial BUY may cancel on Exit; preserve all spent orders."""
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _owner_lock(cur,did)
            cur.execute("""UPDATE mininex_across_staged_orders o SET state='CANCELLED',updated_at=now()
                WHERE o.order_id=%s AND o.privy_did=%s AND o.side='BUY' AND o.stage=1 AND o.state='OPEN'
                  AND EXISTS(SELECT 1 FROM mininex_live_evm_auto_sessions s
                    WHERE s.privy_did=o.privy_did AND s.asset=o.asset AND s.status='EXIT_PENDING')
                  AND NOT EXISTS(SELECT 1 FROM mininex_across_staged_steps t WHERE t.order_id=o.order_id
                    AND (t.state IN ('SENDING','SUBMITTED','SOURCE_FINALIZED','UNCERTAIN')
                         OR (t.kind!='APPROVAL' AND t.state='FINALIZED')))
                RETURNING order_id""",(order_id,did))
            return cur.fetchone() is not None
