"""Owner-approved real MiniNex 5% fee: sign/post *exactly once* via Privy.

This internal-only transaction sender requires:
1. an actual, immutable HWM fee obligation from a verified finalized SELL;
2. a fresh owner-wallet Privy policy + exact Treasury grant + remote quorum GET;
3. a live, separately owner-signed and granted fee permission;
4. current stablecoin USD conversion, actual canonical fee token/recipient;
5. a committed permanent SENDING journal and same-chain nonce lock.

It never accepts a client-requested fee, skips consent or retries a timeout.
It is disabled by default until an explicit owner-grant deployment.
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import HTTPException

from .shared_evm_fee_auto_grant_confirm import (
    get_owner_fee_auto_remote_grant_audit,
)
from .shared_evm_auto_fee_send_claim import (
    claim_one_owner_authorized_fee,record_original_auto_fee_send,
)
from .shared_evm_fee_transfer_proof import owner_fee_treasury
from .shared_evm_policy_reference_rates import (
    fetch_evm_policy_reference_rates,_price,
)
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .eth_privy_p256_signer import sign_privy_rpc_payload
from .shared_evm_swap_receipts import _pair
from .storage import _safe_did
from .live_orders import valid_order_key

LOG=logging.getLogger("mininex.owner_fee_auto_submit")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI"))


def automatic_fee_transfer_worker_enabled()->bool:
    """External deployment switch, not a trading amount/day cap."""
    return (
        os.getenv("MININEX_EVM_AUTO_FEES_ENABLED")=="explicit-owner-privy-grant"
        and os.getenv("MININEX_LIVE_TRADING_ENABLED")=="true"
        and bool(os.getenv("DATABASE_URL","").strip())
    )


async def submit_original_owner_auto_fee_once(
    *,did:str,asset:str,execution_id:str,client:httpx.AsyncClient,
)->dict:
    did=_safe_did(did)
    key=valid_order_key(execution_id)
    if (asset not in _ASSETS or type(client) is not httpx.AsyncClient
            or not automatic_fee_transfer_worker_enabled()):
        raise HTTPException(409,"Owner auto fee payments not explicitly enabled")
    remote=await get_owner_fee_auto_remote_grant_audit(
        did=did,asset=asset)
    if (remote.get("owner_fee_grant_remotely_verified") is not True
            or remote.get("signer_quorum_remotely_verified") is not True
            or remote.get("asset")!=asset):
        raise HTTPException(409,"No independently verified owner fee grant")
    stable=remote.get("stable")
    _pair(asset,stable,"BUY")
    treasury=owner_fee_treasury(asset=asset,stable=stable)
    if remote.get("treasury_wallet")!=treasury:
        raise HTTPException(409,"Fee Treasury changed since owner approval")
    rates=await fetch_evm_policy_reference_rates(asset,stable)
    rate=_price(rates,stable,int(time.time()))
    # Permanent original SENDING claim commits BEFORE signature or POST.
    # A timeout/crash after this point may already represent money in flight.
    original=claim_one_owner_authorized_fee(
        did=did,asset=asset,execution_id=key,remote=remote,
        stable_usd=rate,treasury=treasury,now=int(time.time()))
    tx_hash=None
    try:
        signed=sign_privy_rpc_payload(
            url=original["url"],body=original["body"],
            idempotency_key=original["idempotency_key"])
        tx_hash=await post_claimed_privy_evm_rpc(
            url=original["url"],body=original["body"],
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signed,
            idempotency_key=original["idempotency_key"],
            client=client)
    except Exception:
        # No retry even if signature was rejected locally or Privy timed out.
        # An unknown API result could already have sent tokens on-chain.
        try:
            record_original_auto_fee_send(
                did=did,asset=asset,execution_id=key,
                digest=original["request_digest"],tx_hash=None)
        except Exception:
            LOG.error("Original unknown fee send must be manually reconciled")
        raise HTTPException(
            503,"Original owner fee transfer outcome uncertain; no retry") from None
    saved=record_original_auto_fee_send(
        did=did,asset=asset,execution_id=key,
        digest=original["request_digest"],tx_hash=tx_hash)
    return {
        "mode":"OWNER_GRANTED_EVM_FEE_AUTO_ONE_SHOT",
        "asset":asset,"execution_id":key,"state":saved["state"],
        "original_tx_hash":tx_hash,
        "onchain_finalized":False,
        "fee_paid_verified":False,
        "automatic_retry_allowed":False,
    }
