"""Read-only, exact mined-time costs for central Ethereum automatic fills.

Approval and reverted-swap gas is attributed once to the next successful
same owner/asset/wallet/side/stable swap, within its original send interval.
This is an explicit bookkeeping convention, not a claim of causal linkage.
Unresolved, ambiguous or unpriced costs defer the valuation. No fee sends.
"""
from __future__ import annotations
import asyncio
import logging
from threading import Lock
from fractions import Fraction
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .storage import _db_url,_ensure_schema,_safe_did
from .shared_evm_swap_receipts import _pair,check_user_evm_swap
from .ethereum_settlement_status import _finalized_receipt,_eq,_num
from .shared_evm_historical_usd import fetch_recent_historical_usd_observation
from .staged_net_valuation import mined_evidence,verified_rate,ratio
from .realized_net_accounting import exact,historical_cost_usd

ASSETS=("ETH","LINK","QNT","UNI","BTC","XRP","SOL","TRX")
LOG=logging.getLogger("mininex.ethereum_net_valuation")
_LOCK=Lock()
_OFFSET=0

def pending_ethereum_valuations(limit=8):
    global _OFFSET
    db=_db_url();_ensure_schema(db)
    sql="""SELECT f.*,b.created_at AS broadcast_at
        FROM mininex_live_evm_verified_fills f
        JOIN mininex_live_evm_auto_broadcasts b ON b.privy_did=f.privy_did
          AND b.asset=f.asset AND b.intent_key=f.intent_key AND b.tx_hash=f.tx_hash
          AND b.wallet_address=f.wallet_address AND b.chain_id=1 AND b.status='FINALIZED_SUCCESS'
        LEFT JOIN mininex_ethereum_net_valuations v ON v.privy_did=f.privy_did
          AND v.asset=f.asset AND v.intent_key=f.intent_key
        WHERE f.asset IN ('ETH','LINK','QNT','UNI','BTC','XRP','SOL','TRX') AND f.chain_id=1
          AND v.intent_key IS NULL ORDER BY f.created_at,f.tx_hash LIMIT %s OFFSET %s"""
    with _LOCK:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,_OFFSET));rows=cur.fetchall()
                if not rows and _OFFSET:
                    _OFFSET=0;cur.execute(sql,(limit,0));rows=cur.fetchall()
        _OFFSET+=len(rows)
    return rows

def read_original_cost_steps(fill):
    """Only exact original journals, never estimate gas from an allowance."""
    did,asset,wallet,side,stable,at=(fill[k] for k in
        ("privy_did","asset","wallet_address","side","stable","broadcast_at"))
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT MAX(b.created_at) AS previous
                FROM mininex_live_evm_auto_broadcasts b
                JOIN mininex_live_evm_verified_fills f ON f.privy_did=b.privy_did
                  AND f.asset=b.asset AND f.intent_key=b.intent_key AND f.tx_hash=b.tx_hash
                WHERE b.privy_did=%s AND b.asset=%s AND b.wallet_address=%s AND b.side=%s
                  AND b.stable=%s AND b.chain_id=1 AND b.status='FINALIZED_SUCCESS'
                  AND b.created_at<%s""",(did,asset,wallet,side,stable,at))
            previous=cur.fetchone()["previous"]
            cur.execute("""SELECT tx_hash,status,token_address,router_address,amount_atomic,approval_mode
                FROM mininex_live_evm_router_approval_sends
                WHERE privy_did=%s AND asset=%s AND wallet_address=%s AND side=%s AND stable=%s
                  AND chain_id=1 AND created_at<=%s AND (%s::timestamptz IS NULL OR created_at>%s)
                ORDER BY created_at,idempotency_key""",(did,asset,wallet,side,stable,at,previous,previous))
            approvals=[dict(r,kind="APPROVAL") for r in cur.fetchall()]
            cur.execute("""SELECT tx_hash,status FROM mininex_live_evm_auto_broadcasts
                WHERE privy_did=%s AND asset=%s AND wallet_address=%s AND side=%s AND stable=%s
                  AND chain_id=1 AND created_at<%s AND (%s::timestamptz IS NULL OR created_at>%s)
                  AND status<>'FINALIZED_SUCCESS' ORDER BY created_at,intent_key""",
                (did,asset,wallet,side,stable,at,previous,previous))
            failures=[dict(r,kind="SWAP") for r in cur.fetchall()]
            conversions=[]
            if asset=="ETH":
                cur.execute("""SELECT c.conversion_tx_hash AS tx_hash,c.swap_tx_hash,c.side,c.stable,c.wallet_address
                    FROM mininex_live_evm_completed_native_cycles c
                    WHERE c.privy_did=%s AND c.asset='ETH' AND c.intent_key=%s AND c.chain_id=1""",
                    (did,fill["intent_key"]))
                cycle=cur.fetchone()
                if (not cycle or cycle["swap_tx_hash"]!=fill["tx_hash"] or cycle["side"]!=side
                        or cycle["stable"]!=stable or cycle["wallet_address"]!=wallet):
                    raise ValueError("Original native ETH cycle not settled")
                conversions=[dict(cycle,kind="CONVERSION",status="FINALIZED_SUCCESS")]
                # Failed native conversions require their own cost linkage;
                # do not understate the costs of a historical ETH trade.
                cur.execute("""SELECT 1 FROM mininex_live_evm_native_sell_intents
                    WHERE privy_did=%s AND asset='ETH' AND wallet_address=%s
                      AND stable=%s AND status='FINALIZED_REVERTED' AND created_at<=%s
                      AND (%s::timestamptz IS NULL OR created_at>%s) LIMIT 1""",
                    (did,wallet,stable,at,previous,previous))
                if cur.fetchone():
                    raise ValueError("Unassigned native conversion costs")
                cur.execute("""SELECT 1 FROM mininex_live_evm_native_auto_steps
                    WHERE privy_did=%s AND asset='ETH' AND wallet_address=%s
                      AND stable=%s AND status='FINALIZED_REVERTED' AND created_at<=%s
                      AND (%s::timestamptz IS NULL OR created_at>%s) LIMIT 1""",
                    (did,wallet,stable,at,previous,previous))
                if cur.fetchone():
                    raise ValueError("Unassigned native withdrawal costs")
    return approvals+failures+conversions

def check_swap_amounts(fill,proof):
    if (proof.get("state")!="FINALIZED_ERC20_SWAP_LEG"
            or proof.get("transaction_finalized") is not True or proof.get("swap_erc20_leg_verified") is not True
            or proof.get("tx_hash")!=fill["tx_hash"] or proof.get("wallet_address")!=fill["wallet_address"]
            or proof.get("asset")!=fill["asset"] or proof.get("side")!=fill["side"]
            or proof.get("stable")!=fill["stable"] or proof.get("chain_id")!=1
            or any(exact(fill[a])!=exact(proof[b]) for a,b in (
                ("source_amount","source_spent_amount"),("destination_amount","destination_received_amount"),
                ("network_fee_native","network_fee_native")))):
        raise ValueError("Original finalized Ethereum fill differs")

async def check_cost_transaction(client,fill,step):
    h=step["tx_hash"];kind=step["kind"]
    if not h or step["status"] not in ("FINALIZED_SUCCESS","FINALIZED_REVERTED"):
        raise ValueError("Unresolved original Ethereum cost step")
    result=await _finalized_receipt(client,1,h)
    if result is None:
        raise ValueError("Original Ethereum cost receipt not finalized")
    tx,receipt=result;wallet=fill["wallet_address"]
    status=_num(receipt["status"])
    if status not in (0,1) or bool(status)!=(step["status"]=="FINALIZED_SUCCESS") or not _eq(tx.get("from"),wallet):
        raise ValueError("Original Ethereum cost owner or outcome differs")
    pair=_pair(fill["asset"],fill["stable"],fill["side"])
    if kind=="APPROVAL":
        if step["approval_mode"] not in ("ROUTER02_FINITE","WRAPPED_USDT_ZERO"):
            raise ValueError("Unknown approval cost mode")
        if not _eq(step["token_address"],pair["source_token"]) or not _eq(step["router_address"],pair["router"]):
            raise ValueError("Original approval route changed")
        amount=0 if step["approval_mode"]=="WRAPPED_USDT_ZERO" else int(step["amount_atomic"])
        data="0x095ea7b3"+pair["router"][2:].lower().rjust(64,"0")+format(amount,"064x")
        if not _eq(tx.get("to"),pair["source_token"]) or not _eq(tx.get("input"),data) or _num(tx["value"])!=0:
            raise ValueError("Original approval calldata differs")
    elif kind=="SWAP":
        proof=await check_user_evm_swap(fill["privy_did"],h,asset=fill["asset"],side=fill["side"],stable=fill["stable"])
        if proof.get("state")!="FINALIZED_REVERTED" or proof.get("wallet_address")!=wallet:
            raise ValueError("Original reverted swap proof differs")
    elif kind=="CONVERSION":
        weth=pair["output_token"] if fill["side"]=="BUY" else pair["source_token"]
        quantity=exact(fill["destination_amount"] if fill["side"]=="BUY" else fill["source_amount"])*10**18
        if quantity.denominator!=1 or fill["asset"]!="ETH" or not _eq(tx.get("to"),weth):
            raise ValueError("Original ETH conversion quantity differs")
        data="0x2e1a7d4d"+format(int(quantity),"064x") if fill["side"]=="BUY" else "0xd0e30db0"
        value=0 if fill["side"]=="BUY" else int(quantity)
        if not _eq(tx.get("input"),data) or _num(tx["value"])!=value:
            raise ValueError("Original ETH conversion calldata differs")
    else:
        raise ValueError("Unknown original Ethereum cost kind")
    return h

async def value_ethereum_order(client,fill,steps):
    if fill["asset"] not in ASSETS or fill["chain_id"]!=1:
        raise ValueError("Unknown original Ethereum fill")
    proof=await check_user_evm_swap(fill["privy_did"],fill["tx_hash"],asset=fill["asset"],side=fill["side"],stable=fill["stable"])
    check_swap_amounts(fill,proof)
    hashes=[fill["tx_hash"]]
    for step in steps:
        hashes.append(await check_cost_transaction(client,fill,step))
    if len(hashes)!=len(set(hashes)):
        raise ValueError("Duplicate Ethereum cost transaction")
    costs=[];rates={};blocks=[];observations=[]
    for h in hashes:
        block=await mined_evidence(client,1,h)
        if block["from"]!=fill["wallet_address"]:
            raise ValueError("Original Ethereum cost wallet differs")
        observation=await fetch_recent_historical_usd_observation(asset="ETH",block_unix=block["block_unix"],client=client)
        rates[(1,h,"ETH")]=verified_rate(observation,asset="ETH",block=block)
        costs.append({"chain_id":1,"tx_hash":h,"gas_atomic":block["gas_atomic"],"native_bridge_fee_atomic":"0"})
        blocks.append(block)
        observations.append({"chain_id":1,"tx_hash":h,"observation":observation})
    if exact(costs[0]["gas_atomic"])!=exact(fill["network_fee_native"])*10**18:
        raise ValueError("Original swap gas differs")
    stable=fill["stable"]
    obs=await fetch_recent_historical_usd_observation(asset=stable,block_unix=blocks[0]["block_unix"],client=client)
    rate=verified_rate(obs,asset=stable,block=blocks[0])
    observations.append({"chain_id":1,"tx_hash":fill["tx_hash"],"observation":obs})
    amount=exact(fill["source_amount"] if fill["side"]=="BUY" else fill["destination_amount"])*10**6
    if amount.denominator!=1:
        raise ValueError("Noninteger original stable debit or credit")
    cost=historical_cost_usd(costs,rates)
    if cost is None:
        raise ValueError("Incomplete Ethereum historical costs")
    return {"version":1,"side":fill["side"],"stable":stable,"stable_atomic":str(int(amount)),
            "stable_usd":ratio(rate),"network_cost_usd":ratio(cost),"costs":costs,
            "blocks":blocks,"observations":observations,
            "approval_allocation":"NEXT_SUCCESSFUL_SAME_SCOPE_SWAP"}

def persist_ethereum_valuation(fill,evidence):
    did=_safe_did(fill["privy_did"]);asset=fill["asset"];key=fill["intent_key"]
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE""",(did,))
            if not cur.fetchone():
                raise ValueError("Original owner missing")
            cur.execute("""SELECT tx_hash,side,stable,source_amount,destination_amount
                FROM mininex_live_evm_verified_fills WHERE privy_did=%s AND asset=%s AND intent_key=%s AND chain_id=1""",
                (did,asset,key))
            row=cur.fetchone()
            if not row or row["tx_hash"]!=fill["tx_hash"] or row["side"]!=evidence["side"] or row["stable"]!=evidence["stable"]:
                raise ValueError("Original Ethereum valuation scope changed")
            amount=exact(row["source_amount"] if row["side"]=="BUY" else row["destination_amount"])*10**6
            if amount!=int(evidence["stable_atomic"]):
                raise ValueError("Original Ethereum stable amount changed")
            cur.execute("""INSERT INTO mininex_ethereum_net_valuations(privy_did,asset,intent_key,tx_hash,stable,evidence)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (privy_did,asset,intent_key) DO NOTHING RETURNING intent_key""",
                (did,asset,key,row["tx_hash"],row["stable"],Jsonb(evidence)))
            if not cur.fetchone():
                return False
            for cost in evidence["costs"]:
                cur.execute("""INSERT INTO mininex_ethereum_net_cost_assignments(tx_hash,privy_did,asset,intent_key)
                    VALUES (%s,%s,%s,%s)""",(cost["tx_hash"],did,asset,key))
            return True

async def reconcile_ethereum_net_valuations_once(client):
    for fill in await asyncio.to_thread(pending_ethereum_valuations):
        try:
            steps=await asyncio.to_thread(read_original_cost_steps,fill)
            evidence=await value_ethereum_order(client,fill,steps)
            await asyncio.to_thread(persist_ethereum_valuation,fill,evidence)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Original Ethereum historical valuation not yet provable")
