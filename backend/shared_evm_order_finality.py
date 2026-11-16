"""Bind a finalized, owner-verified EVM swap receipt to its durable order.

Works for BNB, POL, AVAX, LINK and UNI without inventing a completed
native conversion, executing a transfer, crediting a position, or charging
the 5% realized profit fee. Reorgs / missing receipt can never be final.
"""
from __future__ import annotations

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_orders import valid_order_key
from .shared_evm_swap_receipts import check_user_evm_swap,_pair
from .storage import _db_url,_ensure_schema,_safe_did

_SUPPORTED=frozenset(("BNB","POL","AVAX","LINK","UNI"))


def _finalize_locked(
    cur, *,did:str,key:str,asset:str,proof:dict,
)->str:
    if type(proof) is not dict or asset not in _SUPPORTED:
        raise ValueError("Invalid EVM finality proof")
    side=proof.get("side")
    stable=proof.get("stable")
    pair=_pair(asset,stable,side)
    if (proof.get("mode")!="SHARED_EVM_FINALIZED_SWAP_VERIFICATION"
            or proof.get("asset")!=asset
            or proof.get("network")!=pair["network"]
            or proof.get("chain_id")!=pair["chain_id"]
            or proof.get("transaction_finalized") is not True
            or proof.get("state") not in ("FINALIZED_ERC20_SWAP_LEG","FINALIZED_REVERTED")
            or type(proof.get("tx_hash")) is not str
            or len(proof["tx_hash"])!=66
            or proof.get("real_order_recorded") is not False
            or proof.get("performance_fee_assessed") is not False):
        raise ValueError("On-chain EVM swap finality not verified")
    success=proof["state"]=="FINALIZED_ERC20_SWAP_LEG"
    if (proof.get("swap_erc20_leg_verified") is not success
            or (success and (
                proof.get("source_spent_amount") is None
                or proof.get("destination_received_amount") is None))):
        raise ValueError("EVM swap token transfers not verified")
    cur.execute(
        """SELECT status,tx_hash,side,stable,chain_id,wallet_address
           FROM mininex_live_evm_auto_broadcasts
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict
            or row.get("status")!="SUBMITTED"
            or row.get("side")!=side
            or row.get("stable")!=stable
            or row.get("chain_id")!=pair["chain_id"]
            or row.get("tx_hash")!=proof["tx_hash"]):
        raise HTTPException(409,"EVM order and receipt do not match")
    state="FINALIZED_SUCCESS" if success else "FINALIZED_REVERTED"
    cur.execute(
        """UPDATE mininex_live_evm_auto_broadcasts
           SET status=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='SUBMITTED' AND tx_hash=%s""",
        (state,did,asset,key,proof["tx_hash"]))
    return state


async def reconcile_evm_internal_order(
    *,did:str,asset:str,intent_key:str,tx_hash:str,
    side:str,stable:str,
)->dict:
    """Internal worker-only: look up actual finalized owner wallet receipt."""
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    if asset not in _SUPPORTED:
        raise ValueError("EVM journal only supports additional coins")
    # check_user_evm_swap retrieves the actual transaction from an HTTPS RPC
    # and binds it to the authenticated Privy-owned wallet. No fake bools.
    proof=await check_user_evm_swap(
        did,tx_hash,asset=asset,side=side,stable=stable)
    if proof.get("transaction_finalized") is not True:
        return {"asset":asset,"intent_key":key,"state":"AWAITING_FINALITY",
                "trade_settled":False,"fee_assessed":False}
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                state=_finalize_locked(cur,did=did,key=key,
                                       asset=asset,proof=proof)
    except psycopg.Error:
        raise HTTPException(503,"Cannot persist EVM receipt finality") from None
    return {"asset":asset,"intent_key":key,"state":state,
            "swap_erc20_leg_finalized":state=="FINALIZED_SUCCESS",
            "native_cycle_settled":False,
            "trade_settled":False,"fee_assessed":False}
