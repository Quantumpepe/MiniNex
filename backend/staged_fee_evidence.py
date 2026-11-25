"""Read-only original staged transaction cost evidence for net accounting.

All monetary movement is handled elsewhere. Fees embedded in the actual
bridge exchange rate are already included in the final received quantity;
only gas and separately paid native bridge value are added here.
"""
import re
from .realized_net_accounting import NATIVE_GAS,exact

_HASH=re.compile(r"^0x[0-9a-f]{64}$")

def staged_cost_evidence(rows,*,asset,order_id,did):
    if asset not in ("BNB","POL","AVAX","ARB"):
        raise ValueError("Unsupported staged cost asset")
    allowed={1,{"BNB":56,"POL":137,"AVAX":43114,"ARB":42161}[asset]}
    costs=[]
    seen=set()
    for row in rows:
        if (row["order_id"]!=order_id or row["privy_did"]!=did
                or row["chain_id"] not in allowed or type(row["chain_id"]) is not int
                or row["state"] not in ("FINALIZED","REVERTED")
                or row["kind"] not in ("APPROVAL","BRIDGE","SWAP")
                or row["stage"] not in (1,2)
                or type(row["tx_hash"]) is not str or not _HASH.fullmatch(row["tx_hash"])):
            raise ValueError("Original cost step scope differs")
        key=(row["chain_id"],row["tx_hash"])
        if key in seen:
            raise ValueError("Duplicate original cost step")
        seen.add(key)
        proof=row["proof"]
        if type(proof) is not dict or proof.get("source_hash")!=row["tx_hash"]:
            raise ValueError("Original cost receipt identity missing")
        expected={"APPROVAL":"APPROVAL_FINALIZED","BRIDGE":"BRIDGE_CREDIT_VERIFIED","SWAP":"SWAP_CREDIT_VERIFIED"}[row["kind"]]
        if proof.get("state")!=(expected if row["state"]=="FINALIZED" else "SOURCE_REVERTED"):
            raise ValueError("Original cost receipt outcome differs")
        gas=exact(proof["source_gas_atomic"])
        fee=exact(proof.get("native_bridge_fee_atomic",0))
        if asset=="AVAX" and row["kind"]=="BRIDGE" and row["state"]=="FINALIZED":
            # Old receipts without this field are not treated as free.
            if "native_bridge_fee_atomic" not in proof or fee!=exact(row["prepared"]["native_bridge_fee_atomic"]):
                raise ValueError("Original native bridge fee evidence missing")
        elif fee:
            raise ValueError("Unexpected separately paid bridge fee")
        if gas.denominator!=1 or fee.denominator!=1:
            raise ValueError("Noninteger original cost atomics")
        costs.append({"chain_id":row["chain_id"],"native_asset":NATIVE_GAS[row["chain_id"]],
                      "tx_hash":row["tx_hash"],"gas_atomic":str(int(gas)),
                      "native_bridge_fee_atomic":str(int(fee))})
    return costs

def read_staged_accounting_order(did,*,asset,order_id):
    from .storage import _db_url,_ensure_schema,_safe_did
    import psycopg
    from psycopg.rows import dict_row
    import uuid
    did=_safe_did(did);order_id=uuid.UUID(str(order_id))
    if asset not in ("BNB","POL","AVAX","ARB"):
        raise ValueError("Unsupported staged cost asset")
    table="mininex_avax_staged" if asset=="AVAX" else "mininex_across_staged"
    condition="'AVAX'=%s" if asset=="AVAX" else "asset=%s"
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"""SELECT * FROM {table}_orders
                WHERE privy_did=%s AND order_id=%s AND {condition} AND state='FINALIZED'""",
                (did,order_id,asset))
            order=cur.fetchone()
            if not order:
                return None
            cur.execute(f"""SELECT * FROM {table}_steps
                WHERE privy_did=%s AND order_id=%s
                ORDER BY stage,created_at,step_id""",(did,order_id))
            rows=cur.fetchall()
    if (not rows or not all(r["state"] in ("FINALIZED","REVERTED") for r in rows)
            or not all(any(r["stage"]==stage and r["kind"]!="APPROVAL" and r["state"]=="FINALIZED"
                           for r in rows) for stage in (1,2))):
        return None
    costs=staged_cost_evidence(rows,asset=asset,order_id=order_id,did=did)
    return {"order":dict(order,asset=asset),"steps":rows,"costs":costs}


def read_staged_cost_evidence(did,*,asset,order_id):
    original=read_staged_accounting_order(did,asset=asset,order_id=order_id)
    return original["costs"] if original is not None else None
