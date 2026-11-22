"""Single-attempt Privy AUTO sender shared by five additional EVM assets.

Internal only; NO FastAPI endpoint, market scheduler, automatic activation,
policy POST or Privy delegation. The owner must first separately approve
the additional signer in Privy, sign the coin/budget EIP191 enrollment and
start their own LIVE session. A trusted strategy worker must previously
reserve the exact server-produced quote under its own coin budget.

A committed SENDING claim is permanent. HTTP timeouts are ambiguous and
NEVER cause another RPC submission or reuse of the same strategy signal.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_orders import valid_order_key
from .storage import _db_url,_ensure_schema,_safe_did
from .shared_evm_swap_preparation import prepare_owner_evm_swap
from .shared_evm_swap_receipts import _pair
from .shared_evm_order_journal import (
    immutable_evm_send_identity,_claim_authorized_evm_order_locked,
    record_internal_authorized_evm_send_result,
)
from .shared_evm_remote_grant_audit import (
    get_owner_evm_remote_grant_audit,_read_grant_record,
)
from .shared_evm_policy_swap_gate import check_evm_policy_bound_swap
from .shared_evm_privy_transport import (
    bound_privy_evm_swap_request,post_claimed_privy_evm_rpc,
)
from .shared_evm_privy_signer import sign_privy_evm_swap_request

LOG=logging.getLogger("mininex.evm_privy_order_submit")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","QNT","UNI","ARB"))


def _fingerprint_prepared_quote(prepared:dict)->str:
    """Hash an actual current owner/DEX quote, never an indicative price."""
    if (type(prepared) is not dict
            or prepared.get("mode")!="SHARED_EVM_OWNER_SWAP_PREPARATION"
            or prepared.get("exact_swap_abi_and_amount_verified") is not True
            or prepared.get("wallet_identity_verified") is not True
            or prepared.get("source_funds_sufficient") is not True
            or prepared.get("router_allowance_sufficient") is not True
            or prepared.get("native_gas_available") is not True
            or prepared.get("owner_wallet_signing_possible") is not True
            or prepared.get("execution_enabled") is not False
            or prepared.get("can_submit") is not False
            or type(prepared.get("unsigned_transaction")) is not dict
            or type(prepared.get("transaction_expires_at")) is not int
            or not int(time.time())<prepared["transaction_expires_at"]<=int(time.time())+45):
        raise HTTPException(409,"Current verified owner swap quote unavailable")
    identity={
        key:prepared.get(key) for key in (
            "asset","chain_id","side","stable","wallet_address",
            "input_amount","source_token_address","output_token_address",
            "transaction_expires_at","unsigned_transaction",
        )
    }
    return hashlib.sha256(json.dumps(
        identity,sort_keys=True,separators=(",",":"),
        ensure_ascii=True,allow_nan=False).encode("ascii")).hexdigest()


def _check_exact_live_order(
    *,asset:str,side:str,stable:str,amount:str,
    owned_record:dict,prepared:dict,
)->tuple[dict,str,str]:
    if asset not in _ASSETS or side not in ("BUY","SELL"):
        raise HTTPException(422,"No enabled EVM AUTO coin or direction")
    pair=_pair(asset,stable,side)
    if (type(owned_record) is not dict
            or owned_record.get("asset")!=asset
            or owned_record.get("stable")!=stable
            or owned_record.get("chain_id")!=pair["chain_id"]
            or owned_record.get("status")!="GRANT_VERIFIED"
            or owned_record.get("revoked_at") is not None
            or prepared.get("asset")!=asset
            or prepared.get("network")!=pair["network"]
            or prepared.get("chain_id")!=pair["chain_id"]
            or prepared.get("side")!=side
            or prepared.get("stable")!=stable
            or prepared.get("input_amount")!=amount
            or prepared.get("wallet_address")!=owned_record.get("wallet_address")):
        raise HTTPException(409,"Owner order, Privy policy and quote differ")
    digest=_fingerprint_prepared_quote(prepared)
    tx=prepared["unsigned_transaction"]
    data=tx.get("data")
    if type(data) is not str or len(data)!=458:
        raise HTTPException(409,"Unverified exact V3 transaction")
    # Decode the exact atomic values from the source DEX calldata. Never
    # substitute a human decimal, asset market estimate or client minOut.
    atomic_input=str(int(data[10+4*64:10+5*64],16))
    atomic_output_min=str(int(data[10+5*64:10+6*64],16))
    check_evm_policy_bound_swap(
        record=owned_record,asset=asset,side=side,stable=stable,
        wallet=owned_record["wallet_address"],tx=tx,
        input_atomic=atomic_input,
        minimum_output_atomic=atomic_output_min,now=int(time.time()))
    return tx,atomic_input,atomic_output_min


async def submit_internal_privy_evm_order(
    *,did:str,asset:str,intent_key:str,side:str,stable:str,
    amount:str,budget_revision:int,client:httpx.AsyncClient,
    prepared_quote:dict|None=None,
    central:bool=False,
)->dict:
    """Send exactly once after independent real quote, consent and grant.

    The quote digest must ALREADY have been reserved in the correct user-
    chosen coin budget under this intent. No app API or scheduler invokes
    this operation until actual consented live session orchestration exists.
    """
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    if (type(asset) is not str or asset not in _ASSETS
            or type(budget_revision) is not int or budget_revision<1
            or type(client) is not httpx.AsyncClient):
        raise ValueError("Untrusted internal EVM execution context")
    # This independently reads signed owner intent, wallet metadata, Privy
    # remote policy rules, restricted additional signer and P-256 quorum.
    report=(await get_owner_evm_remote_grant_audit(
        did,asset,central=True,
    ) if central else await get_owner_evm_remote_grant_audit(did,asset))
    if (report.get("asset")!=asset
            or report.get("owner_wallet_current") is not True
            or report.get("exact_remote_policy_rules_verified") is not True
            or report.get("privy_additional_signer_scoped") is not True
            or report.get("backend_quorum_current") is not True
            or report.get("signed_coin_budget_intent_in_ledger") is not True):
        raise HTTPException(409,"Current owner-approved Privy grant not verified")
    owned_record=await __import__("asyncio").to_thread(
        _read_grant_record,did,asset,central=True,
    ) if central else await __import__("asyncio").to_thread(
        _read_grant_record,did,asset,
    )
    # This function alone does NOT activate a user session; the trusted
    # caller additionally requires a user-started live trading session.
    # A trusted strategy may pass the exact server-generated quote that it
    # has already durably reserved. Requoting would change its expiry and
    # break the immutable quote digest even though the signal is unchanged.
    # This is an INTERNAL function, not a client-supplied HTTP parameter.
    prepared=(prepared_quote if prepared_quote is not None
              else await prepare_owner_evm_swap(
                  did,asset=asset,side=side,stable=stable,amount=amount))
    tx,input_atomic,minimum_out=_check_exact_live_order(
        asset=asset,side=side,stable=stable,amount=amount,
        owned_record=owned_record,prepared=prepared)
    quote_sha=_fingerprint_prepared_quote(prepared)
    identity=immutable_evm_send_identity(
        asset=asset,wallet_id=owned_record["wallet_id"],
        wallet=owned_record["wallet_address"],tx=tx,
        side=side,stable=stable,input_atomic=input_atomic,
        minimum_output_atomic=minimum_out)
    url,body=bound_privy_evm_swap_request(
        asset=asset,wallet_id=owned_record["wallet_id"],
        wallet=owned_record["wallet_address"],tx=tx,
        side=side,stable=stable,input_atomic=input_atomic,
        minimum_output_atomic=minimum_out)
    # Commit the permanent once-only SENDING claim before signer and POST.
    # This transaction rechecks original owner consent, current grant,
    # settings revision, exact prepared quote and capital reservation.
    try:
        db=_db_url()
        _ensure_schema(db)
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _claim_authorized_evm_order_locked(
                    cur,did=did,key=key,identity=identity,
                    quote_sha256=quote_sha,budget_revision=budget_revision,
                    **({"central":True} if central else {}))
    except psycopg.Error:
        raise HTTPException(503,"Cannot durably claim user-approved EVM order") from None
    tx_hash=None
    try:
        signed=sign_privy_evm_swap_request(
            asset=asset,wallet_id=owned_record["wallet_id"],
            wallet=owned_record["wallet_address"],tx=tx,
            side=side,stable=stable,input_atomic=input_atomic,
            minimum_output_atomic=minimum_out,idempotency_key=key)
        tx_hash=await post_claimed_privy_evm_rpc(
            url=url,body=body,
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signed,idempotency_key=key,
            client=client)
    except Exception:
        # At most one network POST may have been made. Its result might
        # still be mined even if Privy returned HTTP 503/timeout.
        record_internal_authorized_evm_send_result(
            did=did,asset=asset,intent_key=key,
            digest=identity["request_digest"],tx_hash=None)
        raise
    record_internal_authorized_evm_send_result(
        did=did,asset=asset,intent_key=key,
        digest=identity["request_digest"],tx_hash=tx_hash)
    return {
        "asset":asset,"intent_key":key,"state":"SUBMITTED",
        "tx_hash":tx_hash,"real_privy_broadcast":True,
        "onchain_finalized":False,
        "native_trade_cycle_settled":False,
        "performance_fee_assessed":False,
    }
