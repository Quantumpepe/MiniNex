"""Durable user-defined independent budget reservations for EVM coins.

Internal only. A user budget is the only trading capital ceiling: no MiniNex
default per-order or per-day cap. A successfully reserved order does NOT
automatically authorize Privy signing, broadcast or wallet balance spending.
The caller must supply an independently server-generated signal/quote digest;
client requests cannot access this writer.
"""
from __future__ import annotations

from decimal import Decimal
import logging
import re

import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException

from .live_orders import valid_order_key,valid_notional
from .storage import _db_url,_ensure_schema,_safe_did
from .shared_evm_swap_receipts import _pair
from .shared_evm_central_runtime import _locked_original_evm_coin
from .ethereum_settlement_funding import assert_ethereum_funds_locked

LOG=logging.getLogger("mininex.evm_budget_reserve")
_SHA=re.compile(r"^[0-9a-f]{64}$")
_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","XRP","SOL","TRX"))


def _reserve_locked(cur,*,did:str,asset:str,key:str,side:str,stable:str,
                    amount_usd:str,quote_sha256:str,
                    expected_revision:int,central:bool=False,
                    ethereum_funding:dict|None=None)->dict:
    """Serialize one coin's reservations by locking its OWN preference row.

    Other coins are entirely independent; their selected budgets never
    become spendable by this one. Missing live per-coin permission fails.
    """
    if asset not in _ASSETS or side not in ("BUY","SELL") or (asset in ("BTC","XRP","SOL","TRX") and not central):
        raise ValueError("Unverified EVM coin or trade direction")
    _pair(asset,stable,side)
    key=valid_order_key(key)
    if type(quote_sha256) is not str or not _SHA.fullmatch(quote_sha256):
        raise ValueError("Verified quote digest required")
    if type(expected_revision) is not int or expected_revision<1:
        raise ValueError("User budget revision required")
    amount=valid_notional(amount_usd)
    if ethereum_funding is not None:
        if side!="BUY" or asset not in ("ETH","LINK","QNT","UNI","BTC","XRP","SOL","TRX"):
            raise ValueError("Unexpected Ethereum funding evidence")
        cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(did,))
        if cur.fetchone()!={"privy_did":did}:
            raise HTTPException(409,"Original Ethereum owner unavailable")
        assert_ethereum_funds_locked(cur,did=did,asset=asset,stable=stable,**ethereum_funding)
    # Central wallet-lock order must precede preference rows, matching
    # extension/revocation and order claim transaction ordering.
    if central:
        _locked_original_evm_coin(
            cur,did=did,asset=asset,stable=stable,
            revision=expected_revision,
        )
    if central and asset=="BTC":
        from .shared_evm_wallet_policy_prepare import _locked_selected_budget
        amount_selected,revision=_locked_selected_budget(cur,did=did,asset=asset,
                                   stable=stable,expected_revision=expected_revision)
        settings={"budget_usd":amount_selected,"revision":revision,"stable":stable}
    elif central and asset=="ETH":
        from .shared_evm_central_eth_settings import locked_original_eth_settings
        settings=locked_original_eth_settings(
            cur,did=did,stable=stable,revision=expected_revision)
    else:
        cur.execute(
            """SELECT budget_usd,stable,revision
               FROM mininex_extra_coin_preferences
               WHERE privy_did=%s AND asset=%s FOR UPDATE""",
            (did,asset))
        settings=cur.fetchone()
    if (type(settings) is not dict
            or type(settings.get("budget_usd")) not in (int,Decimal)
            or settings["budget_usd"]<=0
            or settings.get("revision")!=expected_revision
            or settings.get("stable")!=stable):
        raise HTTPException(409,"Independent coin budget changed or unavailable")
    if not central:
        cur.execute(
            """SELECT status,expires_at,revoked_at,stable,
                      wallet_id,wallet_address,consent_sha256
               FROM mininex_live_evm_owner_permissions
               WHERE privy_did=%s AND asset=%s FOR UPDATE""",
            (did,asset))
        permission=cur.fetchone()
        from datetime import datetime,timezone
        if (type(permission) is not dict
                or permission.get("status")!="GRANT_VERIFIED"
                or permission.get("stable")!=stable
                or permission.get("revoked_at") is not None
                or not isinstance(permission.get("expires_at"),datetime)
                or permission["expires_at"].tzinfo is None
                or permission["expires_at"]<=datetime.now(timezone.utc)):
            raise HTTPException(409,"This coin lacks verified, active user permission")
        # The remote signer grant is not proof of the owner's EIP-191 intent.
        # Bind both to the exact original wallet, budget revision, settlement
        # and immutable signed text. A changed preference needs new consent.
        cur.execute(
            """SELECT status,wallet_id,wallet_address,chain_id,stable,
                      budget_revision,budget_usd,message_sha256,confirmed_at
               FROM mininex_live_evm_consent_challenges
               WHERE privy_did=%s AND asset=%s FOR UPDATE""",
            (did,asset))
        consent=cur.fetchone()
        if (type(consent) is not dict
                or consent.get("status")!="SIGNED_INTENT"
                or consent.get("chain_id")!=_pair(asset,stable,side)["chain_id"]
                or consent.get("stable")!=stable
                or consent.get("budget_revision")!=expected_revision
                or Decimal(str(consent.get("budget_usd")))!=Decimal(settings["budget_usd"])
                or consent.get("confirmed_at") is None
                or consent.get("wallet_id")!=permission.get("wallet_id")
                or consent.get("wallet_address")!=permission.get("wallet_address")
                or consent.get("message_sha256")!=permission.get("consent_sha256")):
            raise HTTPException(409,"Current wallet-signed coin budget consent required")
    # BUY consumes the owner's selected per-coin buying budget.
    # SELL reduces an existing spot position: its appreciated USD value
    # must not become an artificial trading cap. Every SELL still requires
    # exact owner consent, source-token balance, Privy policy and a durable
    # once-only claim, but it does not spend fresh stablecoin capital.
    # An uncertain BUY still consumes its reservation until reconciled.
    cur.execute(
        """SELECT COALESCE(SUM(amount_usd),0) AS reserved_usd
           FROM mininex_live_evm_budget_reservations
           WHERE privy_did=%s AND asset=%s AND side='BUY'
             AND state IN ('RESERVED','SENDING','SUBMITTED',
                           'RECONCILIATION_REQUIRED')""",
        (did,asset))
    reserved_row=cur.fetchone()
    if type(reserved_row) is not dict or reserved_row.get("reserved_usd") is None:
        raise HTTPException(503,"Cannot verify this coin's reserved capital")
    reserved=Decimal(str(reserved_row["reserved_usd"]))
    if reserved<0 or (side=="BUY" and
                      reserved+amount>Decimal(settings["budget_usd"])):
        raise HTTPException(409,"User-selected coin budget already reserved")
    # A previous key is never reused, even after it becomes finalized.
    cur.execute(
        """INSERT INTO mininex_live_evm_budget_reservations
           (privy_did,asset,intent_key,stable,side,budget_revision,
            amount_usd,quote_sha256)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(privy_did,asset,intent_key) DO NOTHING
           RETURNING state""",
        (did,asset,key,stable,side,expected_revision,amount,quote_sha256))
    claimed=cur.fetchone()
    if type(claimed) is not dict or claimed.get("state")!="RESERVED":
        raise HTTPException(409,"Strategy signal already reserved; no replay")
    return {
        "asset":asset,"intent_key":key,"reserved_usd":format(amount,"f"),
        "budget_revision":expected_revision,"state":"RESERVED",
        "privy_send_authorized":False,"real_trade_sent":False,
    }


def reserve_internal_evm_coin_budget(
    *,did:str,asset:str,intent_key:str,side:str,stable:str,
    amount_usd:str,quote_sha256:str,expected_revision:int,
    central:bool=False,ethereum_funding:dict|None=None,
)->dict:
    """Internal strategy path only. No user-facing writer or scheduler."""
    did=_safe_did(did)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _reserve_locked(cur,did=did,asset=asset,
                    key=intent_key,side=side,stable=stable,
                    amount_usd=amount_usd,quote_sha256=quote_sha256,
                    expected_revision=expected_revision,
                    central=central,ethereum_funding=ethereum_funding)
    except psycopg.Error:
        LOG.warning("Independent EVM budget reservation unavailable")
        raise HTTPException(503,"EVM coin budget reservation unavailable") from None


def _cancel_unsubmitted_locked(
    cur,*,did:str,asset:str,key:str,quote_sha256:str,
)->bool:
    """Release ONLY an untouched RESERVED quote; never an attempted send.

    Lock the original account row first, exactly as the Privy SENDING
    claimant does, so a concurrently starting broadcast cannot race this
    decision. The cancelled signal key remains terminal forever.
    """
    if (asset not in _ASSETS or type(quote_sha256) is not str
            or not _SHA.fullmatch(quote_sha256)):
        raise ValueError("Unverified EVM budget cancellation")
    key=valid_order_key(key)
    cur.execute(
        """SELECT privy_did FROM mininex_accounts
           WHERE privy_did=%s FOR UPDATE""",(did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"Owner account is not available")
    cur.execute(
        """SELECT status FROM mininex_live_evm_auto_broadcasts
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,key))
    sent=cur.fetchone()
    if sent is not None:
        # A SENDING, SUBMITTED, uncertain or finalized transaction always
        # remains subject to on-chain reconciliation, never cancellation.
        return False
    cur.execute(
        """UPDATE mininex_live_evm_budget_reservations
           SET state='CANCELLED',updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND quote_sha256=%s AND state='RESERVED'
           RETURNING state""",(did,asset,key,quote_sha256))
    changed=cur.fetchone()
    return type(changed) is dict and changed.get("state")=="CANCELLED"


def cancel_internal_unsubmitted_evm_reservation(
    *,did:str,asset:str,intent_key:str,quote_sha256:str,
)->bool:
    """Internal cleanup after PRE-broadcast failure, never a wallet refund."""
    did=_safe_did(did)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _cancel_unsubmitted_locked(
                    cur,did=did,asset=asset,key=intent_key,
                    quote_sha256=quote_sha256)
    except psycopg.Error:
        LOG.warning("Failed to release an unsubmitted EVM quote reservation")
        raise HTTPException(503,"Cannot safely release original EVM reservation") from None
