"""Attach finalized approval gas to the next matching original swap.

No new transaction, fee or capital release. Costs are charged once in
chronological order to the next same-coin, same-direction, same-token
swap. Incomplete historical evidence prevents a verified net result.
"""
import asyncio
from decimal import Decimal,localcontext,ROUND_CEILING
import re

import psycopg
from psycopg.rows import dict_row
from .storage import _db_url,_ensure_schema,_safe_did
from .shared_evm_historical_usd import fetch_recent_historical_usd_observation
from .shared_evm_fifo_net import _historical_price

_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def _load(did,asset,wallet):
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT side,stable,chain_id,tx_hash,status,
                  confirmed_gas_atomic,gas_block_number,gas_block_hash,gas_block_unix
                FROM mininex_live_evm_router_approval_sends
                WHERE privy_did=%s AND asset=%s AND wallet_address=%s
                  AND approval_mode='ROUTER02_FINITE'
                  AND status IN ('FINALIZED_SUCCESS','FINALIZED_REVERTED')
                ORDER BY gas_block_number NULLS FIRST,tx_hash""",
                (_safe_did(did),asset,wallet))
            return list(cur.fetchall())


def plan_approval_costs(rows,fills):
    """Allocate each expense once; same-block order is not assumed."""
    assigned=[];seen=set();unallocated=0
    for row in rows:
        h=row["tx_hash"];b=row["gas_block_number"];g=row["confirmed_gas_atomic"]
        if (type(h) is not str or not _HASH.fullmatch(h) or h in seen
                or row["status"] not in ("FINALIZED_SUCCESS","FINALIZED_REVERTED")
                or type(b) is not int or b<=0 or g is None
                or type(row["gas_block_unix"]) is not int or row["gas_block_unix"]<=0
                or not _HASH.fullmatch(row["gas_block_hash"] or "")):
            raise ValueError("Original finalized approval cost evidence incomplete")
        seen.add(h)
        gas=Decimal(g)
        if not gas.is_finite() or gas<0 or gas!=gas.to_integral_value():
            raise ValueError("Invalid original approval gas")
        candidates=[i for i,f in enumerate(fills) if f["side"]==row["side"]
            and f["stable"]==row["stable"] and f["chain_id"]==row["chain_id"]
            and f["block_number"]>=b]
        if not candidates:
            unallocated+=1
            continue
        i=candidates[0]
        if fills[i]["block_number"]==b:
            raise ValueError("Same-block approval order not proven")
        if row["chain_id"]!=1:
            raise ValueError("Non-Ethereum approval full gas cost not yet reconciled")
        assigned.append((i,row))
    return assigned,unallocated


async def attach_original_approval_costs(*,did,asset,wallet,fills,client,now=None):
    rows=await asyncio.to_thread(_load,did,asset,wallet)
    assigned,pending=plan_approval_costs(rows,fills)
    totals={}
    for i,row in assigned:
        proof=await fetch_recent_historical_usd_observation(
            asset="ETH",block_unix=row["gas_block_unix"],client=client,now=now)
        rate=_historical_price(proof,"ETH",row["gas_block_unix"])
        with localcontext() as ctx:
            ctx.prec=80
            cost=Decimal(row["confirmed_gas_atomic"]).scaleb(-18)*rate
            totals[i]=totals.get(i,Decimal(0))+cost
    # Apply only when the complete assigned cost set was successfully priced.
    for i,fill in enumerate(fills):
        fill["allocated_approval_gas_usd"]=format(totals.get(i,Decimal(0)).quantize(
            Decimal("0.000000000001"),rounding=ROUND_CEILING),"f")
    return {"allocated_approvals":len(assigned),"pending_approvals":pending}
