"""Read-only automatic candidate preparation from the real owner wallet.

No broadcast, no remote policy modification, no live reservation. A legacy
Router02 grant cannot authorize the new bridge router or source chain.
"""
from decimal import Decimal, localcontext
import asyncio
from fastapi import HTTPException

from .ethereum_settlement_plan import settlement_route, ethereum_buy_amount, ethereum_sell_amount
from .ethereum_settlement_quote import get_ethereum_cross_evm_quote
from .ethereum_settlement_journal import load_quote
from .ethereum_settlement_preflight import prepare_bridge_privy_request
from .shared_evm_central_runtime import audit_original_evm_coin, original_evm_coin_record
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import _owned_eth_wallet
from .wallet_balances import _eth_rpc_call
from .multicoin_evm_balances import _read_chain, _WRAPPED_GROUPS
from .storage import _safe_did


def exact_balance(entry,decimals):
    if not isinstance(entry,dict) or entry.get("status")!="verified":
        raise ValueError("Verified source balance unavailable")
    with localcontext() as ctx:
        ctx.prec=100
        value=Decimal(entry["balance"])*10**decimals
        if not value.is_finite() or value<0 or value!=value.to_integral_value():
            raise ValueError("Invalid balance atomics")
        return int(value)


async def read_cross_evm_funding(did,asset,stable,client):
    did=_safe_did(did)
    route=settlement_route(asset,"BUY",stable)
    if route["family"]!="CROSS_EVM":
        raise ValueError("Expected cross-EVM asset")
    user=await fetch_privy_user(did)
    owner=_owned_eth_wallet(user,did)
    wallet=owner["address"]
    if await _eth_rpc_call(client,"eth_chainId",[],1)!=1:
        raise ValueError("Wrong Ethereum RPC")
    token=route["settlement_contract"]
    balance=await _eth_rpc_call(client,"eth_call",[
        {"to":token,"data":"0x70a08231"+wallet[2:].lower().rjust(64,"0")},"latest"],2)
    source=await _read_chain(client,"ARB_TOKENS" if asset=="ARB" else asset,wallet)
    native=exact_balance(source.get(asset),18)
    wrapped=0
    if asset in _WRAPPED_GROUPS:
        symbol,_,_=_WRAPPED_GROUPS[asset]
        wrapped=exact_balance(source.get(symbol),18)
    return {"wallet_id":owner["wallet_id"],"wallet":wallet,
            "stable_atomic":str(balance),"coin_atomic":str(native+wrapped),
            "native_atomic":str(native),"wrapped_atomic":str(wrapped)}


async def prepare_cross_evm_auto_candidate(*,did,asset,side,stable,budget_revision,
        amount,client,now):
    """Prepare exact route for a server-originated signal; old consent is
    audited but never treated as the new policy's signing permission.

    The worker must supply an amount from the shared funding calculation and
    independently verified strategy-position ledger. No public API accepts it.
    """
    did=_safe_did(did)
    route=settlement_route(asset,side,stable)
    if route["family"]!="CROSS_EVM":
        raise ValueError("Expected cross-EVM settlement candidate")
    audit=await audit_original_evm_coin(did,asset)
    record=await asyncio.to_thread(original_evm_coin_record,did,asset)
    if (audit["current_user_budget_revision"]!=budget_revision
        or record["budget_revision"]!=budget_revision or record["stable"]!=stable
        or record["policy_sha256"]!=audit["remote_verified_policy_sha256"]
        or record["wallet_id"]!=audit["remote_verified_wallet_id"]):
        raise HTTPException(409,"Owner-approved coin budget changed")
    quote=await get_ethereum_cross_evm_quote(did,route["source_asset"],route["destination_asset"],amount)
    if not quote["restricted_calldata_verified"]:
        return {"asset":asset,"state":"ROUTE_INDICATIVE_ONLY","can_submit":False}
    persisted=await asyncio.to_thread(load_quote,did,quote["quote_id"])
    if persisted["wallet_address"]!=record["wallet_address"].lower():
        raise HTTPException(409,"Original wallet differs")
    import time
    plan=await prepare_bridge_privy_request(client,persisted,
        wallet_id=record["wallet_id"],now=max(now,int(time.time())),manual=True)
    return {"asset":asset,"quote_id":quote["quote_id"],"side":side,
            "state":"CROSS_CHAIN_POLICY_MIGRATION_REQUIRED",
            "approval_needed":bool(plan.get("manual_approval")),
            "estimated_source_gas_atomic":plan["estimated_gas_atomic"],
            "owner_budget_revision":budget_revision,
            "source_chain_id":route["source_chain_id"],
            "destination_chain_id":route["destination_chain_id"],
            "can_submit":False,"real_trade_sent":False}
