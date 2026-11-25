"""Original one-shot BNB/POL/AVAX native -> wrapped DEPOSIT before SELL.

No client API, no simulation, no duplicate deposits on ambiguous outcome.
One user-started session, owner-signed coin budget, current Privy remote
policy/quorum, and actual onchain native wallet balance are required.
The original signal id is retained for a later wrapped-token Router02 SELL.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
import os
import re
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_strategy_execution import derive_evm_strategy_signal_key
from .shared_evm_remote_grant_audit import (
    get_owner_evm_remote_grant_audit,_read_grant_record,
    _read_grant_evidence_locked,validate_pinned_evm_policy,_lte,
)
from .shared_evm_allowance import check_evm_source_allowance
from .multicoin_evm_balances import _RPC
from .native_evm_v3_quote import _rpc,_decode_quantity
from .shared_evm_swap_receipts import _pair
from .shared_native_evm_conversion import bound_native_evm_conversion
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .eth_privy_p256_signer import sign_privy_rpc_payload
from .storage import _db_url,_ensure_schema,_safe_did

_NATIVE=frozenset(("ETH","BNB","POL","AVAX"))
_SIG=re.compile(r"^[0-9a-f]{64}$")
_FLAGS=("owner_wallet_current","exact_remote_policy_rules_verified",
        "privy_additional_signer_scoped","backend_quorum_current",
        "signed_coin_budget_intent_in_ledger")


def native_sell_deposit_key(did:str,asset:str,key:str)->str:
    if asset not in _NATIVE or type(key) is not str:
        raise ValueError("Invalid original native SELL")
    return "native_"+hashlib.sha256(json.dumps(
        [_safe_did(did),asset,key,"DEPOSIT"],separators=(",",":")
    ).encode("ascii")).hexdigest()[:56]


def exact_native_sell_wei(*,native_atomic:object,gas_atomic:object,
                          deposit_cap_atomic:object)->int:
    """Use wallet's actual native inventory less independently priced gas.

    gas_atomic includes deposit plus the subsequent swap fee reserve. No
    arbitrary MiniNex trade amount/day ceiling or utilization percentage.
    """
    try:
        balance=Decimal(str(native_atomic))
        gas=Decimal(str(gas_atomic))
        cap=Decimal(str(deposit_cap_atomic))
        if (not all(x.is_finite() for x in (balance,gas,cap))
                or gas<=0 or cap<=0 or balance<=gas
                or any(x!=x.to_integral_value() for x in (balance,gas,cap))):
            raise ValueError("Owner native amount or network gas invalid")
        amount=min(balance-gas,cap)
        if not 0<amount<Decimal(10)**45:
            raise ValueError("No native amount after actual network fees")
        return int(amount)
    except (ArithmeticError,ValueError,TypeError):
        raise ValueError("Actual native EVM capital or gas not verified") from None


async def estimate_native_sell_fee_reserve(
    *,asset:str,stable:str,source_evidence:dict,client:httpx.AsyncClient,
)->int:
    """Estimate real chain fee from live gasPrice + canonical deposit gas.

    Reserve enough native for the deposit and a subsequent Router02 SELL.
    This is a network-fee provision, not a system trading capital ceiling.
    The quoted fee remains in the user's wallet; it is not transferred.
    """
    pair=_pair(asset,stable,"SELL")
    if (asset not in _NATIVE or type(source_evidence) is not dict
            or source_evidence.get("wallet_identity_verified") is not True
            or source_evidence.get("asset")!=asset
            or source_evidence.get("chain_id")!=pair["chain_id"]
            or source_evidence.get("network")!=pair["network"]
            or type(client) is not httpx.AsyncClient):
        raise ValueError("Native SELL owner and network not verified")
    wallet=source_evidence.get("wallet_address")
    if type(wallet) is not str or not re.fullmatch(r"0x[0-9a-f]{40}",wallet):
        raise ValueError("Missing verified owner wallet")
    chain,network,fallback,env=_RPC["ETH_TOKENS" if asset=="ETH" else asset]
    if (chain!=pair["chain_id"] or network!=pair["network"]):
        raise ValueError("Native gas route differs from onchain pair")
    url=os.getenv(env,"").strip() or fallback
    if not url.startswith("https://") or any(ch in url for ch in " \n\r\t#@"):
        raise ValueError("Native EVM RPC must use HTTPS")
    tx={"from":wallet,"to":pair["source_token"],
        "data":"0xd0e30db0","value":"0x1"}
    try:
        responses=await asyncio.gather(
            _rpc(client,url,"eth_chainId",[],31),
            _rpc(client,url,"eth_gasPrice",[],32),
            _rpc(client,url,"eth_estimateGas",[tx],33),
        )
        if _decode_quantity(responses[0])!=chain:
            raise ValueError("Native EVM gas quote from another chain")
        gas_price=_decode_quantity(responses[1])
        deposit_gas=_decode_quantity(responses[2])
        if not 0<gas_price<10**15 or not 21_000<=deposit_gas<10**7:
            raise ValueError("Missing real native network gas")
        # Leave fee provision for both transactions. Router02 exact-input
        # cannot be estimated before the native token has been deposited.
        # 350k is a gas-cost planning allowance, NEVER an order/day cap.
        return gas_price*(deposit_gas*2+350_000)
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"Verified native SELL network fee unavailable") from None


async def prepare_original_native_sell_amount(
    *,did:str,asset:str,stable:str,source_evidence:dict,
    client:httpx.AsyncClient,
    central:bool=False,
)->int:
    """Use exact owner native balance minus real gas and owner Privy policy."""
    did=_safe_did(did)
    pair=_pair(asset,stable,"SELL")
    if (asset not in _NATIVE
            or type(source_evidence) is not dict
            or source_evidence.get("mode")!="SHARED_EVM_SOURCE_ALLOWANCE_READ_ONLY"
            or source_evidence.get("asset")!=asset
            or source_evidence.get("side")!="SELL"
            or source_evidence.get("stable")!=stable
            or source_evidence.get("chain_id")!=pair["chain_id"]
            or source_evidence.get("source_token_address")!=pair["source_token"]
            or source_evidence.get("wallet_identity_verified") is not True
            or type(source_evidence.get("native_gas_balance_atomic")) is not str):
        raise ValueError("Unverified native wallet inventory")
    record=await asyncio.to_thread(
        _read_grant_record,did,asset,
        **({"central":True} if central else {}),
    )
    if (record.get("wallet_address")!=source_evidence.get("wallet_address")
            or record.get("stable")!=stable
            or record.get("chain_id")!=pair["chain_id"]):
        raise HTTPException(409,"Native SELL wallet grant differs from owner")
    if central:
        from .shared_evm_central_runtime import central_policy_rule
        native_rule=central_policy_rule(
            record,asset=asset,kind="native_deposit",
            stable=stable,now=int(time.time()),
        )
    else:
        expected=validate_pinned_evm_policy(
            record,asset=asset,now=int(time.time()),
        )
        native_rule=expected["rules"][2]
    cap=_lte(native_rule,"value")
    gas=await estimate_native_sell_fee_reserve(
        asset=asset,stable=stable,source_evidence=source_evidence,client=client)
    return exact_native_sell_wei(
        native_atomic=source_evidence["native_gas_balance_atomic"],
        gas_atomic=gas,deposit_cap_atomic=cap)


def _claim_native_sell_locked(
    cur,*,did:str,asset:str,key:str,run_id:str,event_id:str,
    record:dict,amount_wei:int,digest:str,budget_revision:int,
    central:bool=False,remote_verified_policy_sha256:str|None=None,
)->None:
    if (asset not in _NATIVE or type(record) is not dict
            or type(amount_wei) is not int or not 0<amount_wei<10**45
            or type(digest) is not str or not _SIG.fullmatch(digest)):
        raise ValueError("Unverified native SELL deposit claim")
    stable=record.get("stable")
    pair=_pair(asset,stable,"SELL")
    if (key!=derive_evm_strategy_signal_key(
            did=did,asset=asset,side="SELL",stable=stable,
            strategy_run_id=run_id,signal_event_id=event_id)):
        raise ValueError("Native original strategy signal changed")
    if central:
        from .shared_evm_central_runtime import (
            _locked_original_evm_coin,central_policy_rule,
        )
        grant=_locked_original_evm_coin(
            cur,did=did,asset=asset,stable=stable,
            revision=budget_revision,
        )
        if (type(remote_verified_policy_sha256) is not str
                or grant["policy_sha256"]!=remote_verified_policy_sha256):
            raise HTTPException(409,"Original native Privy remote scope changed")
        native_rule=central_policy_rule(
            grant,asset=asset,kind="native_deposit",
            stable=stable,now=int(time.time()),
        )
    else:
        grant=_read_grant_evidence_locked(cur,did=did,asset=asset)
        policy=validate_pinned_evm_policy(
            grant,asset=asset,now=int(time.time()),
        )
        native_rule=policy["rules"][2]
    cap=int(_lte(native_rule,"value"))
    if (grant.get("wallet_id")!=record.get("wallet_id")
            or grant.get("wallet_address")!=record.get("wallet_address")
            or grant.get("policy_sha256")!=record.get("policy_sha256")
            or grant.get("chain_id")!=pair["chain_id"]
            or grant.get("budget_revision")!=budget_revision
            or amount_wei>cap):
        raise HTTPException(409,"Native deposit exceeds owner-approved coin policy")
    cur.execute(
        """SELECT status,stable,chain_id,wallet_address,budget_revision
           FROM mininex_live_evm_auto_sessions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    session=cur.fetchone()
    if (type(session) is not dict or not (session.get("status")=="ACTIVE" or
            (session.get("status")=="EXIT_PENDING" and central and asset=="ETH"))
            or session.get("stable")!=stable
            or session.get("chain_id")!=pair["chain_id"]
            or session.get("wallet_address")!=record["wallet_address"]
            or session.get("budget_revision")!=budget_revision):
        raise HTTPException(409,"Native SELL requires owner's active coin session")
    cur.execute(
        """SELECT privy_did FROM mininex_accounts
           WHERE privy_did=%s FOR UPDATE""",(did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"Native owner wallet account lock missing")
    if central and asset=="ETH":
        from .shared_evm_exit_inventory import assert_eth_exit_deposit_locked
        assert_eth_exit_deposit_locked(cur,did,record["wallet_address"],amount_wei)
    for table in ("mininex_live_evm_auto_broadcasts",
                  "mininex_live_evm_native_auto_steps",
                  "mininex_live_evm_native_sell_intents"):
        if table=="mininex_live_evm_auto_broadcasts":
            # Shared nonce across native trading and Treasury fee transfers.
            cur.execute(
                """SELECT COUNT(*) AS pending FROM (
                     SELECT 1 FROM mininex_live_evm_auto_broadcasts
                      WHERE privy_did=%s AND chain_id=%s AND status IN
                       ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                     UNION ALL
                     SELECT 1 FROM mininex_evm_auto_fee_sends_all
                      WHERE privy_did=%s AND chain_id=%s AND status IN
                       ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                     UNION ALL
                     SELECT 1 FROM mininex_live_evm_router_approval_sends
                      WHERE privy_did=%s AND chain_id=%s AND status IN
                       ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                     UNION ALL
                     SELECT 1 FROM mininex_avax_staged_steps
                      WHERE privy_did=%s AND chain_id=%s AND state IN
                       ('SENDING','SUBMITTED','UNCERTAIN')
                     UNION ALL
                     SELECT 1 FROM mininex_across_staged_steps
                      WHERE privy_did=%s AND chain_id=%s AND state IN
                       ('SENDING','SUBMITTED','UNCERTAIN')
                     UNION ALL
                     SELECT 1 FROM mininex_ethereum_settlement_submissions s
                      WHERE s.privy_did=%s AND s.source_chain_id=%s AND s.state IN
                       ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                      AND NOT EXISTS (SELECT 1 FROM mininex_ethereum_settlement_finality f WHERE f.quote_id=s.quote_id)
                   ) AS shared_wallet_nonce""",
                (did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"]))
        else:
            cur.execute(
                f"""SELECT COUNT(*) AS pending FROM {table}
                    WHERE privy_did=%s AND chain_id=%s AND status IN
                      ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
                (did,pair["chain_id"]))
        row=cur.fetchone()
        if type(row) is not dict or row.get("pending")!=0:
            raise HTTPException(409,"Another wallet network transaction unresolved")
    # The previous native BUY may already be swapped but not yet
    # converted into the real native wallet balance. Preserve that
    # original cycle before depositing and selling a new position.
    cur.execute(
        """SELECT COUNT(*) AS pending
           FROM mininex_live_evm_auto_broadcasts b
           LEFT JOIN mininex_live_evm_completed_native_cycles c
             ON c.privy_did=b.privy_did AND c.asset=b.asset
            AND c.intent_key=b.intent_key
           WHERE b.privy_did=%s AND b.chain_id=%s
             AND b.asset=%s AND b.side='BUY' AND b.status='FINALIZED_SUCCESS'
             AND c.intent_key IS NULL""",
        (did,pair["chain_id"],asset))
    unsettled=cur.fetchone()
    if (type(unsettled) is not dict
            or type(unsettled.get("pending")) is not int
            or unsettled["pending"]!=0):
        raise HTTPException(409,"Previous native BUY cycle unsettled")
    # A finalized deposit is not yet a finished SELL. Repeated market
    # ticks must not deposit a second native amount while the first
    # original event has no durable downstream Router02 order.
    cur.execute(
        """SELECT COUNT(*) AS pending
           FROM mininex_live_evm_native_sell_intents n
           LEFT JOIN mininex_live_evm_auto_broadcasts b
             ON b.privy_did=n.privy_did AND b.asset=n.asset
            AND b.intent_key=n.intent_key
           WHERE n.privy_did=%s AND n.asset=%s
             AND n.status='FINALIZED_SUCCESS'
             AND b.intent_key IS NULL""",(did,asset))
    unfinished=cur.fetchone()
    if (type(unfinished) is not dict
            or type(unfinished.get("pending")) is not int
            or unfinished["pending"]!=0):
        raise HTTPException(409,"Prior native SELL awaits its original swap")
    if central:
        cur.execute(
            """INSERT INTO mininex_live_evm_native_sell_intents
               (privy_did,asset,intent_key,strategy_run_id,signal_event_id,
                chain_id,stable,wallet_id,wallet_address,budget_revision,
                amount_wei,request_digest,status,grant_source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       'SENDING','PRIVY_CENTRAL_ONE_SIGNER')""",
            (did,asset,key,run_id,event_id,pair["chain_id"],stable,
             record["wallet_id"],record["wallet_address"],
             budget_revision,amount_wei,digest))
    else:
        cur.execute(
            """INSERT INTO mininex_live_evm_native_sell_intents
               (privy_did,asset,intent_key,strategy_run_id,signal_event_id,
                chain_id,stable,wallet_id,wallet_address,budget_revision,
                amount_wei,request_digest,status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SENDING')""",
            (did,asset,key,run_id,event_id,pair["chain_id"],stable,
             record["wallet_id"],record["wallet_address"],
             budget_revision,amount_wei,digest))


def _save_native_sell_result_locked(
    cur,*,did:str,asset:str,key:str,digest:str,tx_hash:str|None,
)->str:
    state="SUBMITTED" if tx_hash else "RECONCILIATION_REQUIRED"
    cur.execute(
        """UPDATE mininex_live_evm_native_sell_intents
           SET status=%s,tx_hash=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND request_digest=%s AND status='SENDING'
           RETURNING status""",(state,tx_hash,did,asset,key,digest))
    saved=cur.fetchone()
    if type(saved) is not dict or saved.get("status")!=state:
        raise HTTPException(409,"Original native SELL status cannot be changed")
    return state


def _save_native_sell_result(**kwargs):
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _save_native_sell_result_locked(cur,**kwargs)


async def submit_internal_native_evm_sell_deposit(
    *,did:str,asset:str,strategy_run_id:str,signal_event_id:str,
    budget_revision:int,native_amount_wei:int,client:httpx.AsyncClient,
    central:bool=False,
)->dict:
    did=_safe_did(did)
    if (asset not in _NATIVE or type(budget_revision) is not int
            or budget_revision<1 or type(client) is not httpx.AsyncClient):
        raise ValueError("Invalid internal native SELL event")
    report=(await get_owner_evm_remote_grant_audit(
        did,asset,central=True,
    ) if central else await get_owner_evm_remote_grant_audit(did,asset))
    if type(report) is not dict or report.get("asset")!=asset or any(
        report.get(field) is not True for field in _FLAGS
    ):
        raise HTTPException(409,"Current owner-approved native SELL grant missing")
    record=await asyncio.to_thread(
        _read_grant_record,did,asset,
        **({"central":True} if central else {}),
    )
    if central and (
        record["policy_sha256"]!=report.get("remote_verified_policy_sha256")
        or record["wallet_id"]!=report.get("remote_verified_wallet_id")
    ):
        raise HTTPException(409,"Original native SELL wallet remote grant changed")
    key=derive_evm_strategy_signal_key(
        did=did,asset=asset,side="SELL",stable=record["stable"],
        strategy_run_id=strategy_run_id,signal_event_id=signal_event_id)
    amount=format(Decimal(native_amount_wei)/Decimal(10**18),"f")
    check=await check_evm_source_allowance(
        did,asset=asset,side="BUY",stable=record["stable"],
        amount="0.000001" if asset!="BNB" else "0.000000000000000001")
    if (check.get("wallet_address")!=record["wallet_address"]
            or check.get("native_gas_available") is not True
            or type(check.get("native_gas_balance_atomic")) is not str
            or int(check["native_gas_balance_atomic"])<=native_amount_wei):
        raise HTTPException(409,"Real native coin balance and gas unavailable")
    url,body=bound_native_evm_conversion(
        asset=asset,wallet_id=record["wallet_id"],
        wallet=record["wallet_address"],operation="DEPOSIT",
        amount_wei=native_amount_wei)
    digest=hashlib.sha256(json.dumps(
        [url,body],sort_keys=True,separators=(",",":")
    ).encode("ascii")).hexdigest()
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _claim_native_sell_locked(
                    cur,did=did,asset=asset,key=key,
                    run_id=strategy_run_id,event_id=signal_event_id,
                    record=record,amount_wei=native_amount_wei,
                    budget_revision=budget_revision,digest=digest,
                    **({
                        "central":True,
                        "remote_verified_policy_sha256":
                            report["remote_verified_policy_sha256"],
                    } if central else {}))
    except psycopg.Error:
        raise HTTPException(503,"Native SELL original order claim unavailable") from None
    native_key=native_sell_deposit_key(did,asset,key)
    tx_hash=None
    try:
        signature=sign_privy_rpc_payload(
            url=url,body=body,idempotency_key=native_key)
        tx_hash=await post_claimed_privy_evm_rpc(
            url=url,body=body,
            app_secret=os.getenv("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signature,idempotency_key=native_key,
            client=client)
    except Exception:
        await asyncio.to_thread(
            _save_native_sell_result,did=did,asset=asset,key=key,
            digest=digest,tx_hash=None)
        raise
    await asyncio.to_thread(
        _save_native_sell_result,did=did,asset=asset,key=key,
        digest=digest,tx_hash=tx_hash)
    return {"asset":asset,"intent_key":key,
            "operation":"DEPOSIT","state":"SUBMITTED",
            "tx_hash":tx_hash,"native_spot_sell_complete":False,
            "automatic_retry_allowed":False}
