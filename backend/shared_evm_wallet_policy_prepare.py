"""Prepare ONE independently scoped EVM wallet policy from saved user capital.

This is the first stage of the shared-signature Start Trade migration. It
never creates a remote Privy policy, grants a signer, or starts a session.

One original user wallet, one existing backend signer quorum, no per-coin
signer ENV and no arbitrary platform capital ceiling. Historic per-coin
signers/consents are NOT silently overwritten; they need explicit migration.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, localcontext
import logging
import os
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .extra_coin_preferences import get_coin_preferences
from .live_btc_controls import get_live_btc_limits
from .ethereum_wrapped_spot_policy import WRAPPED_SPOT_POLICY, WRAPPED_SPOT_TOKENS, wrapped_spot_route
from .trade_preferences import get_trade_preferences
from .eth_user_consent import _require_policy_roles
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import (
    _owned_eth_wallet, fetch_privy_wallet_metadata,
)
from .shared_evm_policy_reference_rates import (
    fetch_evm_policy_reference_rates, derive_user_budget_evm_atomic_caps,
    derive_ethereum_settlement_atomic_caps, _price,
)
from .shared_evm_fee_transfer_proof import owner_fee_treasury
from .shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy, verified_wallet_policy_digest,
    DIRECT_SETTLEMENT_ASSETS,DIRECT_SETTLEMENT_POLICY,
)
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG=logging.getLogger("mininex.one_signer_policy_prepare")
_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","XRP"))


def _require_unattached_original_wallet(user:dict, did:str, metadata:dict)->dict:
    """Only a verified original user wallet with no old signers is eligible."""
    try:
        owned=_owned_eth_wallet(user,did)
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Original Privy wallet not verified") from None
    if (owned["delegated"] is True
            or type(metadata) is not dict
            or metadata.get("id")!=owned["wallet_id"]
            or metadata.get("chain_type")!="ethereum"
            or str(metadata.get("address","")).lower()!=owned["address"]
            or type(metadata.get("entity")) is not dict
            or metadata["entity"].get("type")!="user"
            or metadata["entity"].get("id")!=did
            or metadata.get("archived_at") is not None
            or metadata.get("additional_signers")!=[]):
        raise HTTPException(
            409,"Existing wallet signers require explicit migration; never overwrite",
        )
    return owned


def _locked_selected_budget(cur,*,did:str,asset:str,stable:str,
                            expected_revision:int)->tuple[object,int]:
    """Read the exact saved settings under the original database row lock."""
    if asset=="BTC":
        cur.execute("""SELECT b.planned_capital_usd,b.revision,t.stable
            FROM mininex_live_btc_limits b
            JOIN mininex_trade_preferences t ON t.privy_did=b.privy_did
            WHERE b.privy_did=%s AND t.asset='BTC' FOR UPDATE OF b,t""",(did,))
        row=cur.fetchone()
        if (type(row) is not dict or row.get("stable")!=stable
                or type(row.get("revision")) is not int
                or row["revision"]!=expected_revision or expected_revision<=0):
            raise HTTPException(409,"Original BTC capital or settlement changed")
        try:
            amount=Decimal(str(row["planned_capital_usd"]))
        except (ValueError,TypeError,ArithmeticError,KeyError):
            raise HTTPException(409,"Original BTC capital unavailable") from None
        if not amount.is_finite() or amount<=0:
            raise HTTPException(409,"Save selected BTC trading capital before Start Trade")
        return amount,row["revision"]
    if asset=="ETH":
        cur.execute(
            """SELECT planned_capital_usd,revision FROM mininex_live_eth_limits
               WHERE privy_did=%s FOR UPDATE""",(did,),
        )
        limits=cur.fetchone()
        cur.execute(
            """SELECT stable,revision FROM mininex_trade_preferences
               WHERE privy_did=%s AND asset='ETH' FOR UPDATE""",(did,),
        )
        choice=cur.fetchone()
        if (type(limits) is not dict or type(choice) is not dict
                or type(limits.get("revision")) is not int
                or limits["revision"]<1
                or type(choice.get("revision")) is not int
                or choice["revision"]<1
                or limits["revision"]!=expected_revision
                or choice.get("stable")!=stable):
            raise HTTPException(409,"Original saved ETH budget changed")
        try:
            amount=Decimal(str(limits["planned_capital_usd"]))
        except (ValueError,TypeError):
            raise HTTPException(409,"Original ETH capital unavailable") from None
        if not amount.is_finite() or amount<=0:
            raise HTTPException(409,"Original ETH capital unavailable")
        return amount,limits["revision"]
    cur.execute(
        """SELECT stable,budget_usd,revision
           FROM mininex_extra_coin_preferences
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset),
    )
    row=cur.fetchone()
    if (type(row) is not dict or row.get("stable")!=stable
            or type(row.get("revision")) is not int
            or row["revision"]<1 or row["revision"]!=expected_revision
            or type(row.get("budget_usd")) is not int
            or row["budget_usd"]<=0):
        raise HTTPException(409,"Save selected original coin capital before Start Trade")
    return row["budget_usd"],row["revision"]


def build_selected_wallet_scope(*,asset,stable,amount,snapshot,now):
    """Persist the route version inside owner-consented scope JSON."""
    if asset in WRAPPED_SPOT_TOKENS:
        route=wrapped_spot_route(asset,stable,"BUY")
        with localcontext() as ctx:
            ctx.prec=100
            if type(amount) not in (int,str,Decimal):
                raise ValueError("Original exact selected trading capital required")
            budget=Decimal(str(amount))
            if not budget.is_finite() or budget<=0:
                raise ValueError("Original exact selected trading capital required")
            buy=int((budget/_price(snapshot,stable,now)*10**route["source_decimals"])
                    .to_integral_value(rounding=ROUND_FLOOR))
            sell=int((budget/_price(snapshot,asset,now)*10**route["destination_decimals"])
                     .to_integral_value(rounding=ROUND_FLOOR))
            if not 0<buy<10**45 or not 0<sell<10**45:
                raise ValueError("Selected wrapped spot budget not representable")
        return {"asset":asset,"stable":stable,
                "treasury":owner_fee_treasury(asset="ETH",stable=stable),
                "buy_source_atomic_cap":str(buy),"sell_source_atomic_cap":str(sell),
                "native_wrap_atomic_cap":None,"owner_max_fee_atomic":"0",
                "expires_at":now+86400,"settlement_policy":WRAPPED_SPOT_POLICY}
    if asset in DIRECT_SETTLEMENT_ASSETS:
        caps=derive_ethereum_settlement_atomic_caps(asset=asset,stable=stable,
            budget_usd=amount,snapshot=snapshot,now=now)
        return {"asset":asset,"stable":stable,
            "treasury":owner_fee_treasury(asset="ETH",stable=stable),
            "buy_source_atomic_cap":caps["buy_source_atomic_cap"],
            "sell_source_atomic_cap":caps["sell_source_atomic_cap"],
            "native_wrap_atomic_cap":None,
            "owner_max_fee_atomic":"0",
            "expires_at":now+86400,
            "settlement_policy":DIRECT_SETTLEMENT_POLICY}
    caps=derive_user_budget_evm_atomic_caps(
        asset=asset,stable=stable,budget_usd=amount,
        snapshot=snapshot,now=now,
    )
    scope={
        "asset":asset,"stable":stable,
        "treasury":owner_fee_treasury(asset=asset,stable=stable),
        "buy_source_atomic_cap":caps["buy_source_atomic_cap"],
        "sell_source_atomic_cap":caps["sell_source_atomic_cap"],
        "native_wrap_atomic_cap":caps["native_wrap_atomic_cap"],
        # This is NOT an actual fee debt. Chain-confirmed 5% HWM debt
        # must be verified again before any eventual fee transfer.
        "owner_max_fee_atomic":caps["buy_source_atomic_cap"],
        "expires_at":now+24*3600,
    }
    return scope


def _stage_one_original_wallet_locked(
    cur,*,did:str,asset:str,stable:str,expected_revision:int,
    snapshot:dict,owned:dict,owner_id:str,signer_id:str,now:int,
)->dict:
    """One irreversible DRAFT claim; never overwrite legacy or remote grants."""
    cur.execute(
        """SELECT privy_did FROM mininex_accounts
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    if not cur.fetchone():
        raise HTTPException(409,"Original user account not provisioned")
    amount,revision=_locked_selected_budget(
        cur,did=did,asset=asset,stable=stable,
        expected_revision=expected_revision,
    )
    # No silent migration from per-coin signer records, even if those are
    # uncertain/pending. A new wallet must be unequivocally unassociated.
    cur.execute(
        """SELECT asset FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s LIMIT 1""",(did,),
    )
    if cur.fetchone():
        raise HTTPException(409,"Legacy EVM signer grants require reconciliation")
    cur.execute(
        """SELECT status FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s LIMIT 1""",(did,),
    )
    if cur.fetchone():
        raise HTTPException(409,"Legacy ETH signer grants require reconciliation")
    scope=build_selected_wallet_scope(asset=asset,stable=stable,amount=amount,
        snapshot=snapshot,now=now)
    kwargs=dict(
        wallet=owned["address"],owner_id=owner_id,executor_id=signer_id,
        scopes=[scope],now=now,
    )
    policy=compose_scoped_evm_wallet_policy(**kwargs)
    digest=verified_wallet_policy_digest(policy,**kwargs)
    cur.execute(
        """INSERT INTO mininex_live_evm_wallet_policy_grants
           (privy_did,wallet_id,wallet_address,owner_id,signer_id,
            policy_sha256,scopes_json,revisions_json,status,
            legacy_reconciliation_required)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'DRAFT',FALSE)
           ON CONFLICT(privy_did) DO NOTHING RETURNING status""",
        (did,owned["wallet_id"],owned["address"],owner_id,signer_id,
         digest,Jsonb({asset:scope}),Jsonb({asset:revision})),
    )
    created=cur.fetchone()
    if type(created) is not dict or created.get("status")!="DRAFT":
        raise HTTPException(409,"Existing original wallet policy requires review")
    return {
        "mode":"ORIGINAL_EVM_ONE_SIGNER_POLICY_DRAFT_ONLY",
        "asset":asset,"chain_type":"ethereum",
        "budget_revision":revision,
        "scope_coins":[asset],
        "scope_rules_count":len(policy["rules"]),
        "policy_sha256":digest,
        "wallet_address":owned["address"],
        "shared_signer_id":signer_id,
        "status":"DRAFT",
        "policy_created_in_privy":False,
        "wallet_additional_signer_granted":False,
        "live_trading_enabled":False,"can_submit":False,
    }


async def prepare_owner_central_evm_wallet_policy(did:str,asset:str)->dict:
    """Owner-authenticated Start Trade stage; server chooses all budget data."""
    did=_safe_did(did)
    if asset not in _ASSETS:
        raise HTTPException(404,"Unsupported shared EVM trading asset")
    owner,signer=_require_policy_roles()
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    metadata=await fetch_privy_wallet_metadata(owned["wallet_id"])
    owned=_require_unattached_original_wallet(user,did,metadata)
    if asset=="ETH":
        settings=get_trade_preferences(did)["settings"]["ETH"]
        # Preflight the actual LIVE selected budget, never PAPER funds.
        # Revision is read by the owner-owned ETH controls, not trade prefs.
        from .live_eth_controls import get_live_eth_limits
        limits=get_live_eth_limits(did)
        revision=limits["revision"]
    elif asset=="BTC":
        settings=get_trade_preferences(did)["settings"]["BTC"]
        revision=get_live_btc_limits(did)["revision"]
    else:
        settings=get_coin_preferences(did)["settings"][asset]
        revision=settings["revision"]
    stable=settings["stable"]
    if stable not in ("USDC","USDT") or type(revision) is not int or revision<1:
        raise HTTPException(409,"Save your coin capital and settlement first")
    snapshot=await fetch_evm_policy_reference_rates(asset,stable)
    now=int(time.time())
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                return _stage_one_original_wallet_locked(
                    cur,did=did,asset=asset,stable=stable,
                    expected_revision=revision,snapshot=snapshot,
                    owned=owned,owner_id=owner,signer_id=signer,now=now,
                )
    except psycopg.Error:
        LOG.warning("Original one-signer EVM policy staging unavailable")
        raise HTTPException(503,"Cannot prepare original wallet policy") from None
