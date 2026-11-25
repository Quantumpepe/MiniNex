"""Canonical Ethereum fee settlement; no second send and no guessed credit."""
import asyncio
import logging
import re
from decimal import Decimal
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .storage import _db_url,_ensure_schema
from .ethereum_settlement_plan import ETHEREUM_STABLES
from .ethereum_settlement_status import _finalized_receipt,_net_token_credit,_eq,_num

LOG=logging.getLogger("mininex.net_fee_receipts")
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ADDR=re.compile(r"^0x[0-9a-f]{40}$")
_TRANSFER="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

def verify_fee_receipt(row,tx,receipt):
    """Caller first independently verifies the canonical finalized block."""
    amount=int(row["amount_atomic"]);wallet=row["wallet_address"];treasury=row["treasury_wallet"]
    token=row["token_address"];h=row["tx_hash"]
    if (row["chain_id"]!=1 or row["stable"] not in ETHEREUM_STABLES or token!=ETHEREUM_STABLES[row["stable"]]
            or not _ADDR.fullmatch(wallet) or not _ADDR.fullmatch(treasury) or wallet==treasury
            or not 0<amount<2**256 or not _HASH.fullmatch(h)):
        raise ValueError("Original fee identity invalid")
    calldata="0xa9059cbb"+format(int(treasury,16),"064x")+format(amount,"064x")
    block=receipt.get("blockHash")
    number=_num(receipt.get("blockNumber"))
    if (not isinstance(block,str) or not _HASH.fullmatch(block.lower()) or number<=0
            or not _eq(tx.get("hash"),h) or not _eq(receipt.get("transactionHash"),h)
            or not _eq(tx.get("blockHash"),block) or _num(tx.get("blockNumber"))!=number
            or not _eq(tx.get("from"),wallet) or not _eq(tx.get("to"),token)
            or not _eq(tx.get("input"),calldata) or _num(tx.get("value"))!=0):
        raise ValueError("Original fee transaction differs from booked transfer")
    logs=receipt.get("logs")
    status=_num(receipt.get("status"))
    if status==0:
        if logs!=[]:
            raise ValueError("Unexpected logs in reverted fee transaction")
        outcome="FINALIZED_REVERTED";paid=0
    elif status==1:
        if _net_token_credit(logs,token,wallet)!=-amount or _net_token_credit(logs,token,treasury)!=amount:
            raise ValueError("Original fee debit or credit differs")
        topics=[_TRANSFER,"0x"+wallet[2:].rjust(64,"0"),"0x"+treasury[2:].rjust(64,"0")]
        matches=[log for log in logs if _eq(log.get("address"),token)
                 and isinstance(log.get("topics"),list)
                 and [str(t).lower() for t in log["topics"]]==topics
                 and _num(log["data"])==amount and log.get("removed") is not True]
        if len(matches)!=1:
            raise ValueError("No unique exact original fee Transfer")
        outcome="PAID_VERIFIED";paid=amount
    else:
        raise ValueError("Unknown original fee receipt status")
    gas=_num(receipt["gasUsed"])*_num(receipt["effectiveGasPrice"])
    if gas<0:
        raise ValueError("Invalid fee receipt gas")
    return {"chain_id":1,"tx_hash":h,"block_hash":block.lower(),"block_number":number,
        "wallet_address":wallet,"treasury_wallet":treasury,"token_address":token,"stable":row["stable"],
        "amount_atomic":str(paid),"outcome":outcome,"network_gas_atomic":str(gas),
        "evidence_sha256":row["evidence_sha256"],"request_digest":row["request_digest"]}

def read_pending_receipts(limit=20):
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT * FROM mininex_net_fee_sends WHERE status='SUBMITTED'
                ORDER BY updated_at,tx_hash LIMIT %s""",(limit,))
            return cur.fetchall()

def persist_receipt(original,proof):
    db=_db_url();_ensure_schema(db)
    key=tuple(original[k] for k in ("privy_did","asset","family","fill_key"))
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(key[0],))
            if not cur.fetchone():raise ValueError("Original fee owner missing")
            cur.execute("""SELECT s.*,o.evidence_sha256 AS obligation_digest,o.amount_atomic AS obligation_amount
                FROM mininex_net_fee_sends s JOIN mininex_net_fee_obligations o
                  ON o.privy_did=s.privy_did AND o.asset=s.asset AND o.family=s.family AND o.fill_key=s.fill_key
                WHERE s.privy_did=%s AND s.asset=%s AND s.family=%s AND s.fill_key=%s FOR UPDATE OF s""",key)
            current=cur.fetchone()
            if (not current or current["status"]!="SUBMITTED"
                    or any(current[k]!=original[k] for k in ("tx_hash","wallet_address","treasury_wallet","token_address",
                        "stable","amount_atomic","evidence_sha256","request_digest"))
                    or current["obligation_digest"]!=current["evidence_sha256"]
                    or current["obligation_amount"]!=current["amount_atomic"]):
                raise ValueError("Original fee receipt journal changed")
            outcome=proof["outcome"]
            if (outcome not in ("PAID_VERIFIED","FINALIZED_REVERTED")
                    or any(current[k]!=proof[k] for k in ("tx_hash","wallet_address","treasury_wallet",
                        "token_address","stable","chain_id","evidence_sha256","request_digest"))
                    or int(proof["amount_atomic"])!=(int(current["amount_atomic"]) if outcome=="PAID_VERIFIED" else 0)):
                raise ValueError("Original verified fee proof differs")
            cur.execute("""INSERT INTO mininex_net_fee_receipts
                (privy_did,asset,family,fill_key,tx_hash,evidence_sha256,stable,amount_atomic,outcome,proof)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (*key,proof["tx_hash"],proof["evidence_sha256"],proof["stable"],int(proof["amount_atomic"]),outcome,Jsonb(proof)))
            cur.execute("""UPDATE mininex_net_fee_sends SET status=%s,updated_at=now()
                WHERE privy_did=%s AND asset=%s AND family=%s AND fill_key=%s
                  AND status='SUBMITTED' AND tx_hash=%s RETURNING fill_key""",(outcome,*key,proof["tx_hash"]))
            if not cur.fetchone():raise ValueError("Original fee receipt finalization changed")

async def reconcile_net_fee_receipts_once(client):
    # No writes or RPCs for a send that does not have a known hash.
    import os
    if not os.getenv("DATABASE_URL","").strip():
        return
    for row in await asyncio.to_thread(read_pending_receipts):
        try:
            original=await _finalized_receipt(client,1,row["tx_hash"])
            if original is not None:
                proof=verify_fee_receipt(row,*original)
                await asyncio.to_thread(persist_receipt,row,proof)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Original fee receipt still unresolved")
