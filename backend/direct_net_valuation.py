"""Historical cost evidence for original pre-staged automatic bridge orders.

No reliance on provider status or a guessed bridge charge. Recheck both
canonical receipts, original route digest and message binding. Old missing
approval evidence remains pending rather than silently creating a fee.
"""
import asyncio
import logging
from threading import Lock
import psycopg
from psycopg.rows import dict_row
from .storage import _db_url,_ensure_schema
from .ethereum_settlement_plan import check_evm_quote_route,NATIVE_ADDRESS
from .ethereum_settlement_calldata import inspect_settlement_route,route_digest,DIAMOND
from .ethereum_settlement_status import _finalized_receipt,_net_token_credit,_native_credit,_num,_eq
from .ethereum_settlement_bridge_proof import prove_across_pair
from .ethereum_settlement_stargate import prove_stargate_pair
from .staged_net_valuation import mined_evidence,verified_rate,ratio,persist_staged_valuation
from .shared_evm_historical_usd import fetch_recent_historical_usd_observation
from .realized_net_accounting import NATIVE_GAS,historical_cost_usd

LOG=logging.getLogger("mininex.direct_net_valuation")
_LOCK=Lock()
_OFFSET=0

def pending_direct_valuations(limit=4):
    global _OFFSET
    db=_db_url();_ensure_schema(db)
    sql="""SELECT q.*,f.source_tx_hash,f.destination_tx_hash,f.received_atomic,f.source_gas_atomic,
            f.bridge_deposit_id,s.created_at AS sent_at
        FROM mininex_ethereum_settlement_quotes q
        JOIN mininex_ethereum_settlement_finality f ON f.quote_id=q.quote_id AND f.privy_did=q.privy_did
        JOIN mininex_ethereum_settlement_submissions s ON s.quote_id=q.quote_id AND s.privy_did=q.privy_did
          AND s.state='SUBMITTED' AND s.source_tx_hash=f.source_tx_hash
        LEFT JOIN mininex_staged_net_valuations v ON v.quote_id=q.quote_id
        WHERE f.outcome='FINALIZED' AND v.quote_id IS NULL AND q.unsigned_transaction IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM mininex_avax_staged_orders o WHERE o.order_id=q.quote_id)
          AND NOT EXISTS(SELECT 1 FROM mininex_across_staged_orders o WHERE o.order_id=q.quote_id)
        ORDER BY f.verified_at,q.quote_id LIMIT %s OFFSET %s"""
    with _LOCK:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,_OFFSET));rows=cur.fetchall()
                if not rows and _OFFSET:
                    _OFFSET=0;cur.execute(sql,(limit,0));rows=cur.fetchall()
        _OFFSET+=len(rows)
    return rows

def original_direct_approvals(q):
    db=_db_url();_ensure_schema(db)
    params=(q["privy_did"],q["asset"],q["wallet_address"],q["side"],q["stable"],q["source_chain_id"],q["sent_at"])
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT MAX(s.created_at) AS previous
                FROM mininex_ethereum_settlement_quotes q
                JOIN mininex_ethereum_settlement_submissions s ON s.quote_id=q.quote_id AND s.privy_did=q.privy_did
                JOIN mininex_ethereum_settlement_finality f ON f.quote_id=q.quote_id AND f.privy_did=q.privy_did
                WHERE q.privy_did=%s AND q.asset=%s AND q.wallet_address=%s AND q.side=%s AND q.stable=%s
                  AND q.source_chain_id=%s AND s.created_at<%s AND f.outcome='FINALIZED'""",params)
            previous=cur.fetchone()["previous"]
            cur.execute("""SELECT tx_hash,status,approval_mode,amount_atomic,token_address,router_address
                FROM mininex_live_evm_router_approval_sends
                WHERE privy_did=%s AND asset=%s AND wallet_address=%s AND side=%s AND stable=%s
                  AND chain_id=%s AND created_at<=%s AND (%s::timestamptz IS NULL OR created_at>%s)
                  AND approval_mode IN ('BRIDGE_FINITE','BRIDGE_USDT_ZERO')
                ORDER BY created_at,idempotency_key""",params+(previous,previous))
            steps=[dict(r,kind="APPROVAL") for r in cur.fetchall()]
            cur.execute("""SELECT q.*,f.source_tx_hash,f.source_gas_atomic
                FROM mininex_ethereum_settlement_quotes q
                JOIN mininex_ethereum_settlement_submissions s ON s.quote_id=q.quote_id AND s.privy_did=q.privy_did
                LEFT JOIN mininex_ethereum_settlement_finality f ON f.quote_id=q.quote_id AND f.privy_did=q.privy_did
                WHERE q.privy_did=%s AND q.asset=%s AND q.wallet_address=%s AND q.side=%s AND q.stable=%s
                  AND q.source_chain_id=%s AND s.created_at<%s AND (%s::timestamptz IS NULL OR s.created_at>%s)
                  AND (f.outcome IS NULL OR f.outcome='SOURCE_REVERTED')""",params+(previous,previous))
            failures=cur.fetchall()
            # No unresolved direct sends can be treated as free gas.
            if any(not r["source_tx_hash"] or r["source_gas_atomic"] is None for r in failures):
                raise ValueError("Unresolved original direct order")
            steps.extend(dict(r,kind="REVERT") for r in failures)
    return steps

async def value_direct_order(client,q,steps):
    check_evm_quote_route(q)
    src,dst=q["source_chain_id"],q["destination_chain_id"]
    wallet=q["wallet_address"]
    source_hash,dest_hash=q["source_tx_hash"],q["destination_tx_hash"]
    original=inspect_settlement_route(q["unsigned_transaction"],terms=q,now=q["issued_at"])
    source=await _finalized_receipt(client,src,source_hash)
    target=await _finalized_receipt(client,dst,dest_hash)
    if not source or not target:
        raise ValueError("Direct original receipts unavailable")
    tx,receipt=source;dtx,dreceipt=target
    if (not _eq(tx.get("from"),wallet) or not _eq(tx.get("to"),DIAMOND)
            or route_digest({**tx,"chainId":src},src)!=q["route_sha256"]
            or _num(receipt["status"])!=1 or _num(dreceipt["status"])!=1):
        raise ValueError("Direct original route differs")
    if q["source_token"]==NATIVE_ADDRESS or _num(tx["value"]):
        native_delta=await _native_credit(client,src,source_hash,tx,wallet)
        if native_delta is None:
            raise ValueError("Original separately paid native fee unavailable")
        native_fee=-native_delta-(int(q["input_atomic"]) if q["source_token"]==NATIVE_ADDRESS else 0)
        if not 0<=native_fee<=original["native_bridge_fee_atomic"]:
            raise ValueError("Direct native debit differs")
    else:
        native_fee=0
    if q["source_token"]!=NATIVE_ADDRESS and -_net_token_credit(receipt["logs"],q["source_token"],wallet)!=int(q["input_atomic"]):
        raise ValueError("Direct source debit differs")
    received=(await _native_credit(client,dst,dest_hash,dtx,wallet) if q["destination_token"]==NATIVE_ADDRESS
              else _net_token_credit(dreceipt["logs"],q["destination_token"],wallet))
    if received!=int(q["received_atomic"]) or received<int(q["minimum_output_atomic"]):
        raise ValueError("Direct credit differs")
    prove=prove_stargate_pair if original["provider"]=="STARGATE_V2" else prove_across_pair
    proof=prove(receipt["logs"],dreceipt["logs"],src=src,dst=dst,wallet=wallet,route=original,received=received)
    if not proof or int(proof["deposit_id"])!=int(q["bridge_deposit_id"]):
        raise ValueError("Direct original bridge identity differs")
    main=await mined_evidence(client,src,source_hash)
    if int(main["gas_atomic"])!=int(q["source_gas_atomic"]):
        raise ValueError("Direct original gas differs")
    costs=[dict(chain_id=src,tx_hash=source_hash,gas_atomic=main["gas_atomic"],native_bridge_fee_atomic=str(native_fee))]
    blocks=[main];seen={source_hash}
    for step in steps:
        h=step.get("tx_hash") if step["kind"]=="APPROVAL" else step["source_tx_hash"]
        if not h or h in seen:
            raise ValueError("Direct cost identity ambiguous")
        result=await _finalized_receipt(client,src,h)
        if not result:
            raise ValueError("Direct cost receipt unavailable")
        t,r=result
        if not _eq(t.get("from"),wallet):
            raise ValueError("Direct cost owner differs")
        status=_num(r["status"])
        if step["kind"]=="APPROVAL":
            amount=0 if step["approval_mode"]=="BRIDGE_USDT_ZERO" else int(step["amount_atomic"])
            calldata="0x095ea7b3"+DIAMOND[2:].rjust(64,"0")+format(amount,"064x")
            if (step["status"] not in ("FINALIZED_SUCCESS","FINALIZED_REVERTED") or status!=int(step["status"]=="FINALIZED_SUCCESS")
                    or not _eq(step["router_address"],DIAMOND) or not _eq(t.get("to"),step["token_address"])
                    or not _eq(t.get("input"),calldata) or _num(t["value"])!=0):
                raise ValueError("Direct approval evidence differs")
        else:
            if status!=0 or route_digest({**t,"chainId":src},src)!=step["route_sha256"]:
                raise ValueError("Direct reverted cost differs")
        block=await mined_evidence(client,src,h)
        if step["kind"]=="REVERT" and int(block["gas_atomic"])!=int(step["source_gas_atomic"]):
            raise ValueError("Direct reverted gas differs")
        costs.append(dict(chain_id=src,tx_hash=h,gas_atomic=block["gas_atomic"],native_bridge_fee_atomic="0"))
        blocks.append(block);seen.add(h)
    rates={};observations=[]
    async def rate(block,asset):
        observation=await fetch_recent_historical_usd_observation(asset=asset,block_unix=block["block_unix"],client=client)
        observations.append(dict(chain_id=block["chain_id"],tx_hash=block["tx_hash"],observation=observation))
        return verified_rate(observation,asset=asset,block=block)
    for block in blocks:
        rates[(src,block["tx_hash"],NATIVE_GAS[src])]=await rate(block,NATIVE_GAS[src])
    total=historical_cost_usd(costs,rates)
    if total is None:
        raise ValueError("Missing historical direct gas rates")
    stable_block=main if q["side"]=="BUY" else await mined_evidence(client,dst,dest_hash)
    if q["side"]=="SELL":
        blocks.append(stable_block)
    stable_rate=await rate(stable_block,q["stable"])
    return dict(version=1,side=q["side"],stable=q["stable"],
        stable_atomic=str(int(q["input_atomic"] if q["side"]=="BUY" else received)),
        stable_usd=ratio(stable_rate),network_cost_usd=ratio(total),costs=costs,
        blocks=blocks,observations=observations,approval_allocation="NEXT_SUCCESSFUL_SAME_SCOPE_BRIDGE")

async def reconcile_direct_net_valuations_once(client):
    for row in await asyncio.to_thread(pending_direct_valuations):
        try:
            steps=await asyncio.to_thread(original_direct_approvals,row)
            value=await value_direct_order(client,row,steps)
            await asyncio.to_thread(persist_staged_valuation,row["privy_did"],row["asset"],row["quote_id"],value)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Historical original direct bridge costs not yet provable")
