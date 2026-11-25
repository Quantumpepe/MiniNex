"""Historical valuation of completed staged Ethereum-settled orders.

A passive worker, no wallet signing, transfers or fee payment. Every gas
cost is tied to the original finalized transaction and its mined block.
Missing historical prices or proof defer valuation; they never become zero.
"""
from __future__ import annotations
import asyncio
import logging
import re
from fractions import Fraction
from threading import Lock
from decimal import Decimal
import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .storage import _db_url,_ensure_schema,_safe_did
from .ethereum_settlement_status import _finalized_receipt,_rpc_url,_eq,_num
from .shared_evm_swap_receipts import _receipt_rpc
from .shared_evm_historical_usd import fetch_recent_historical_usd_observation
from .realized_net_accounting import exact,historical_cost_usd,calculate_realized_net
from .realized_profit_reserve import PROFIT_FILLS_SQL
from .staged_fee_evidence import read_staged_accounting_order

LOG=logging.getLogger("mininex.staged_net_valuation")
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_PAGE_LOCK=Lock()
_OFFSET=0

def ratio(value):
    return [str(value.numerator),str(value.denominator)]

def read_ratio(value,*,positive=False):
    if (type(value) is not list or len(value)!=2 or
            any(type(x) is not str or not x.isascii() or not x.isdecimal() or len(x)>256 for x in value)):
        raise ValueError("Invalid historical valuation ratio")
    if int(value[1])<=0:
        raise ValueError("Invalid historical valuation denominator")
    result=Fraction(int(value[0]),int(value[1]))
    if positive and not result:
        raise ValueError("Missing historical settlement rate")
    return result

def stable_legs(order,steps):
    """One original stable debit on BUY; every verified ETH credit on SELL."""
    selected=[s for s in steps if s["kind"]=="BRIDGE" and s["state"]=="FINALIZED"]
    if order["side"]=="BUY":
        if len(selected)!=1 or selected[0]["stage"]!=1 or selected[0]["chain_id"]!=1:
            raise ValueError("Original Ethereum stable debit missing")
        return [{"chain_id":1,"tx_hash":selected[0]["tx_hash"],
                 "amount_atomic":str(int(order["original_input_atomic"]))}]
    if order["side"]!="SELL" or not selected:
        raise ValueError("Original Ethereum stable credit missing")
    result=[]
    seen=set()
    for step in selected:
        proof=step["proof"];tx=proof.get("destination_hash")
        if step["stage"]!=2 or not isinstance(tx,str) or not _HASH.fullmatch(tx) or tx in seen:
            raise ValueError("Original Ethereum stable credit identity differs")
        seen.add(tx)
        amount=exact(proof["received_atomic"],positive=True)
        if amount.denominator!=1:
            raise ValueError("Noninteger original Ethereum stable credit")
        result.append({"chain_id":1,"tx_hash":tx,"amount_atomic":str(int(amount))})
    if sum(int(x["amount_atomic"]) for x in result)!=int(order["final_received_atomic"]):
        raise ValueError("Original Ethereum stable credit total differs")
    return result

async def mined_evidence(client,chain,tx_hash):
    result=await _finalized_receipt(client,chain,tx_hash)
    if result is None:
        raise ValueError("Original transaction not finalized")
    tx,receipt=result
    number=_num(receipt["blockNumber"])
    block=await _receipt_rpc(client,_rpc_url(chain),"eth_getBlockByNumber",[hex(number),False],9)
    if not block or not _eq(block.get("hash"),receipt["blockHash"]) or _num(block["number"])!=number:
        raise ValueError("Original execution block changed")
    block_hash=receipt["blockHash"].lower()
    if not _HASH.fullmatch(block_hash):
        raise ValueError("Original execution block hash differs")
    status=_num(receipt["status"])
    timestamp=_num(block["timestamp"])
    if status not in (0,1) or number<=0 or timestamp<=0:
        raise ValueError("Invalid original mined execution facts")
    return {"chain_id":chain,"tx_hash":tx_hash,"block_number":number,
        "block_hash":block_hash,"block_unix":timestamp,
        "from":tx["from"].lower(),"value_atomic":str(_num(tx["value"])),
        "gas_atomic":str(_num(receipt["gasUsed"])*_num(receipt["effectiveGasPrice"])),
        "success":status==1}

def verified_rate(observation,*,asset,block):
    if (observation.get("asset")!=asset or observation.get("provider")!="COINGECKO_MARKET_CHART_RANGE"
            or observation.get("execution_block_unix")!=block["block_unix"]
            or type(observation.get("rate_observed_unix")) is not int
            or abs(observation["rate_observed_unix"]-block["block_unix"])>180):
        raise ValueError("Original historical observation differs from mined block")
    return exact(observation["usd_price"],positive=True)

async def value_staged_order(client,original):
    order=original["order"];steps=original["steps"];costs=original["costs"]
    legs=stable_legs(order,steps)
    mined={};rates={};observations=[]
    async def read(chain,tx):
        key=(chain,tx)
        if key not in mined:
            mined[key]=await mined_evidence(client,chain,tx)
        return mined[key]
    async def rate(chain,tx,asset):
        key=(chain,tx,asset)
        if key not in rates:
            block=await read(chain,tx)
            observation=await fetch_recent_historical_usd_observation(
                asset=asset,block_unix=block["block_unix"],client=client)
            rates[key]=verified_rate(observation,asset=asset,block=block)
            observations.append({"chain_id":chain,"tx_hash":tx,"observation":observation})
        return rates[key]
    step_by_key={(s["chain_id"],s["tx_hash"]):s for s in steps}
    for cost in costs:
        chain,tx=cost["chain_id"],cost["tx_hash"]
        block=await read(chain,tx);step=step_by_key[(chain,tx)]
        if (block["from"]!=order["wallet_address"].lower()
                or block["gas_atomic"]!=cost["gas_atomic"]
                or block["success"]!=(step["state"]=="FINALIZED")
                or (block["success"] and block["value_atomic"]!=cost["native_bridge_fee_atomic"])):
            raise ValueError("Actual owner gas/value differs from original step")
        if int(cost["gas_atomic"])+int(cost["native_bridge_fee_atomic"]):
            await rate(chain,tx,cost["native_asset"])
    net_cost=historical_cost_usd(costs,rates)
    if net_cost is None:
        raise ValueError("Original cost rate unavailable")
    total=0;weighted=Fraction(0)
    for leg in legs:
        tx=leg["tx_hash"]
        block=await read(1,tx)
        if not block["success"]:
            raise ValueError("Original Ethereum stable leg failed")
        amount=int(leg["amount_atomic"])
        weighted+=amount*await rate(1,tx,order["stable"])
        total+=amount
    if not total:
        raise ValueError("Original stable amount missing")
    return {"version":1,"side":order["side"],"stable":order["stable"],
            "stable_atomic":str(total),"stable_usd":ratio(weighted/total),
            "network_cost_usd":ratio(net_cost),"costs":costs,"stable_legs":legs,
            "blocks":list(mined.values()),"observations":observations}

def persist_staged_valuation(did,asset,quote_id,evidence):
    did=_safe_did(did)
    if asset not in ("BNB","POL","AVAX","ARB"):
        raise ValueError("Invalid staged valuation asset")
    read_ratio(evidence["stable_usd"],positive=True)
    read_ratio(evidence["network_cost_usd"])
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT q.asset,q.side,q.stable,q.input_atomic,f.received_atomic
                FROM mininex_ethereum_settlement_quotes q
                JOIN mininex_ethereum_settlement_finality f ON f.quote_id=q.quote_id AND f.privy_did=q.privy_did
                WHERE q.quote_id=%s AND q.privy_did=%s AND q.asset=%s AND f.outcome='FINALIZED'""",(quote_id,did,asset))
            row=cur.fetchone()
            if (not row or evidence["version"]!=1 or evidence["side"]!=row["side"]
                    or evidence["stable"]!=row["stable"]
                    or int(evidence["stable_atomic"])!=int(row["input_atomic"] if row["side"]=="BUY" else row["received_atomic"])):
                raise ValueError("Original finalized parent differs from valuation")
            cur.execute("""INSERT INTO mininex_staged_net_valuations(quote_id,privy_did,asset,stable,evidence)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (quote_id) DO NOTHING RETURNING quote_id""",
                (quote_id,did,asset,row["stable"],Jsonb(evidence)))
            return cur.fetchone() is not None

def pending_staged_valuations(limit=4):
    global _OFFSET
    db=_db_url();_ensure_schema(db)
    sql="""SELECT p.privy_did,p.asset,p.order_id FROM (
          SELECT privy_did,'AVAX' AS asset,order_id,updated_at FROM mininex_avax_staged_orders WHERE state='FINALIZED'
          UNION ALL SELECT privy_did,asset,order_id,updated_at FROM mininex_across_staged_orders WHERE state='FINALIZED'
        ) p LEFT JOIN mininex_staged_net_valuations v ON v.quote_id=p.order_id AND v.privy_did=p.privy_did
        WHERE v.quote_id IS NULL ORDER BY p.updated_at,p.order_id LIMIT %s OFFSET %s"""
    with _PAGE_LOCK:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,_OFFSET));rows=cur.fetchall()
                if not rows and _OFFSET:
                    _OFFSET=0;cur.execute(sql,(limit,0));rows=cur.fetchall()
        _OFFSET+=len(rows)
    return rows

async def reconcile_staged_net_valuations_once(client):
    for row in await asyncio.to_thread(pending_staged_valuations):
        try:
            original=await asyncio.to_thread(read_staged_accounting_order,row["privy_did"],
                asset=row["asset"],order_id=row["order_id"])
            if original:
                evidence=await value_staged_order(client,original)
                await asyncio.to_thread(persist_staged_valuation,row["privy_did"],row["asset"],row["order_id"],evidence)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Historical staged order costs not yet provable")

def read_owner_realized_net(did,asset):
    """Read-only actual FIFO net; all prior canonical fills, both stables.

    Staged and central Ethereum valuations use independently verified
    evidence. Older and unpriced histories remain pending.
    """
    did=_safe_did(did)
    from .ethereum_settlement_plan import COIN_NETWORKS
    if asset not in COIN_NETWORKS:
        raise ValueError("Realized net valuation not supported for coin")
    db=_db_url();_ensure_schema(db)
    sql=PROFIT_FILLS_SQL.replace("=%(stable)s"," IN ('USDC','USDT')")
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""WITH fills AS ("""+sql+""")
                SELECT f.*,COALESCE(v.evidence,e.evidence) AS evidence FROM fills f
                LEFT JOIN mininex_staged_net_valuations v ON f.family='CROSS_EVM'
                    AND v.quote_id::text=f.fill_key AND v.privy_did=%(did)s AND v.asset=f.asset AND v.stable=f.stable
                LEFT JOIN mininex_ethereum_net_valuations e ON f.family IN ('ETHEREUM','WRAPPED_SPOT')
                    AND e.tx_hash=f.fill_key AND e.privy_did=%(did)s AND e.asset=f.asset AND e.stable=f.stable
                WHERE f.asset=%(asset)s ORDER BY f.settled_at,f.fill_key""",{"did":did,"asset":asset})
            rows=cur.fetchall()
    for row in rows:
        value=row.pop("evidence")
        if value is not None:
            amount=exact(row["source_amount"] if row["side"]=="BUY" else row["destination_amount"])*10**6
            if (value["version"]!=1 or value["side"]!=row["side"] or value["stable"]!=row["stable"]
                    or amount!=int(value["stable_atomic"])):
                raise ValueError("Stored historical valuation no longer matches original fill")
            row["stable_usd"]=read_ratio(value["stable_usd"],positive=True)
            row["network_cost_usd"]=read_ratio(value["network_cost_usd"])
    return calculate_realized_net(rows)
