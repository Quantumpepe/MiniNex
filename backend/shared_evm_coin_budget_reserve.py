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

LOG=logging.getLogger("mininex.evm_budget_reserve")
_SHA=re.compile(r"^[0-9a-f]{64}$")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI"))


def _reserve_locked(cur,*,did:str,asset:str,key:str,side:str,stable:str,
                    amount_usd:str,quote_sha256:str,
                    expected_revision:int)->dict:
    """Serialize one coin's reservations by locking its OWN preference row.

    Other coins are entirely independent; their selected budgets never
    become spendable by this one. Missing live per-coin permission fails.
    """
    if asset not in _ASSETS or side not in ("BUY","SELL"):
        raise ValueError("Unverified EVM coin or trade direction")
    _pair(asset,stable,side)
    key=valid_order_key(key)
    if type(quote_sha256) is not str or not _SHA.fullmatch(quote_sha256):
        raise ValueError("Verified quote digest required")
    if type(expected_revision) is not int or expected_revision<1:
        raise ValueError("User budget revision required")
    amount=valid_notional(amount_usd)
    cur.execute(
        """SELECT budget_usd,stable,revision
           FROM mininex_extra_coin_preferences
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",
        (did,asset))
    settings=cur.fetchone()
    if (type(settings) is not dict
            or type(settings.get("budget_usd")) is not int
            or settings["budget_usd"]<=0
            or settings.get("revision")!=expected_revision
            or settings.get("stable")!=stable):
        raise HTTPException(409,"Independent coin budget changed or unavailable")
    cur.execute(
        """SELECT status,expires_at,revoked_at,stable
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
    # Include all unresolved reservations, even if the on-chain send status
    # is uncertain. A timeout is never permission to spend that money again.
    cur.execute(
        """SELECT COALESCE(SUM(amount_usd),0) AS reserved_usd
           FROM mininex_live_evm_budget_reservations
           WHERE privy_did=%s AND asset=%s
             AND state IN ('RESERVED','SENDING','SUBMITTED',
                           'RECONCILIATION_REQUIRED')""",
        (did,asset))
    reserved_row=cur.fetchone()
    if type(reserved_row) is not dict or reserved_row.get("reserved_usd") is None:
        raise HTTPException(503,"Cannot verify this coin's reserved capital")
    reserved=Decimal(str(reserved_row["reserved_usd"]))
    if reserved<0 or reserved+amount>Decimal(settings["budget_usd"]):
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
                    expected_revision=expected_revision)
    except psycopg.Error:
        LOG.warning("Independent EVM budget reservation unavailable")
        raise HTTPException(503,"EVM coin budget reservation unavailable") from None
