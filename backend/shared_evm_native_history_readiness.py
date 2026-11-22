"""Real owner-only BNB/POL/AVAX history provenance coordination.

The existing finalized wallet orders are reloaded in their entirety from
Postgres, reconciled with the original onchain Router02 receipts, original
native WETH9 conversion traces, and immutable block-time USD references.
Then an independent paginated trace_filter audit inspects *all observed
native value* during that exact interval, including unknown external funds.

Do not confuse the observed interval with proof of the owner wallet's full
pre-existing native balance and capital basis. This is a READ-ONLY readiness
result, not a licensed 5% High-Water-Mark assessment or an auto-fee trigger.
No trading thresholds, daily caps, signer calls or fund transfers exist here.
"""
from __future__ import annotations

import asyncio

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_fill_history import _verified_fill_row
from .shared_evm_historical_fill_bridge import independently_price_stored_evm_fill
from .shared_evm_native_value_interval_audit import audit_original_native_value_interval
from .storage import _db_url,_ensure_schema,_safe_did

_NATIVE=frozenset(("ETH","BNB","POL","AVAX"))


def _load_all_verified_owner_native_fills(did:str,asset:str)->list[dict]:
    """Original full owner+coin ledger, not the last 30 shown in the UI."""
    did=_safe_did(did)
    if asset not in _NATIVE:
        raise ValueError("Native custody reader only supports BNB/POL/AVAX")
    try:
        db=_db_url()
        _ensure_schema(db)
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT f.asset,f.chain_id,f.intent_key,f.tx_hash,
                              f.wallet_address,f.side,f.stable,
                              f.source_amount,f.destination_amount,
                              f.network_fee_native,f.created_at,
                              b.status AS broadcast_status,
                              b.tx_hash AS broadcast_tx_hash,
                              b.side AS broadcast_side,
                              b.stable AS broadcast_stable,
                              b.wallet_address AS broadcast_wallet_address
                       FROM mininex_live_evm_verified_fills f
                       LEFT JOIN mininex_live_evm_auto_broadcasts b
                         ON b.privy_did=f.privy_did AND b.asset=f.asset
                        AND b.intent_key=f.intent_key
                       WHERE f.privy_did=%s AND f.asset=%s
                       ORDER BY f.created_at ASC,f.intent_key ASC""",(did,asset))
                result=[]
                while True:
                    rows=cur.fetchmany(200)
                    if not rows:break
                    for row in rows:
                        if (row.get("broadcast_status")!="FINALIZED_SUCCESS"
                                or row.get("broadcast_tx_hash")!=row.get("tx_hash")
                                or row.get("broadcast_side")!=row.get("side")
                                or row.get("broadcast_stable")!=row.get("stable")
                                or row.get("broadcast_wallet_address")
                                    !=row.get("wallet_address")):
                            raise ValueError("Unconfirmed or substituted native trade")
                        result.append(_verified_fill_row(row,asset))
        return result
    except psycopg.Error:
        raise HTTPException(
            503,"Original native wallet ledger is not fully available") from None


async def read_owner_native_value_history_readiness(
    *,did:str,asset:str,client:httpx.AsyncClient,
    now:int|None=None,
)->dict:
    """Independent source evidence, no client-supplied fills or prices."""
    did=_safe_did(did)
    if asset not in _NATIVE or type(client) is not httpx.AsyncClient:
        raise ValueError("Original native owner value history required")
    stored=await asyncio.to_thread(_load_all_verified_owner_native_fills,did,asset)
    if not stored:
        return {
            "mode":"OWNER_NATIVE_VALUE_HISTORY_READ_ONLY",
            "asset":asset,"original_finalized_fill_count":0,
            "full_owner_wallet_cost_basis_proven":False,
            "external_wallet_value_audit_complete":False,
            "fee_assessment_eligible":False,
            "hwm_updated":False,"fee_transferred":False,
        }
    wallet=stored[0]["wallet_address"]
    if any(row["wallet_address"]!=wallet for row in stored):
        raise ValueError("Several original native wallets cannot share one FIFO basis")
    mined=[]
    for row in stored:
        proof=await independently_price_stored_evm_fill(
            did=did,row=row,client=client,now=now,durable_rates=True)
        trace=proof.get("native_conversion_trace")
        if (proof.get("mode")!="INDEPENDENT_FINALIZED_EVM_EXECUTION"
                or proof.get("asset")!=asset
                or proof.get("wallet_address")!=wallet
                or proof.get("historic_usd_priced") is not True
                or proof.get("native_cycle_complete") is not True
                or type(trace) is not dict
                or trace.get("original_finalized_internal_value_proven") is not True
                or trace.get("full_historical_wallet_activity_proven") is not False
                or type(proof.get("block_number")) is not int
                or type(proof.get("transaction_index")) is not int):
            raise ValueError("Actual native wallet conversion history incomplete")
        mined.append(proof)
    mined.sort(key=lambda item:(item["block_number"],item["transaction_index"]))
    positions=[(entry["block_number"],entry["transaction_index"]) for entry in mined]
    if len(set(positions))!=len(positions):
        raise ValueError("Duplicate original mined trade position")
    audit=await audit_original_native_value_interval(
        asset=asset,wallet=wallet,fills=mined,client=client)
    if (type(audit) is not dict
            or audit.get("mode")!="READ_ONLY_NATIVE_VALUE_INTERVAL_AUDIT"
            or audit.get("asset")!=asset
            or audit.get("wallet_address")!=wallet
            or audit.get("verified_native_conversion_count")!=len(mined)
            or type(audit.get("unexpected_native_value_movements")) is not list
            or audit.get("complete_wallet_lifetime_and_cost_basis_proven") is not False
            or audit.get("eligible_to_assess_five_percent_fee") is not False
            or audit.get("observed_interval_native_balance_conserved") is not True
            or type(audit.get("original_gas_balance_conservation")) is not dict
            or audit["original_gas_balance_conservation"].get(
                "mode")!="ARCHIVED_NATIVE_WALLET_BALANCE_GAS_CONSERVATION_ONLY"
            or audit["original_gas_balance_conservation"].get(
                "wallet_address")!=wallet
            or audit["original_gas_balance_conservation"].get(
                "observed_interval_native_balance_conserved") is not True
            or audit["original_gas_balance_conservation"].get(
                "five_percent_hwm_writable") is not False
            or audit["original_gas_balance_conservation"].get(
                "archived_sender_nonce_delta_verified") is not True):
        raise ValueError("Original complete interval native audit missing")
    return {
        "mode":"OWNER_NATIVE_VALUE_HISTORY_READ_ONLY",
        "asset":asset,"wallet_address":wallet,
        "original_finalized_fill_count":len(mined),
        "all_original_conversion_trace_calls_verified":True,
        "all_original_fill_block_usd_prices_verified":True,
        "observed_interval_start_block":audit["start_block"],
        "observed_interval_end_block":audit["end_block"],
        "external_value_free_within_observed_interval":
            audit["external_native_value_free_interval"],
        "observed_interval_native_balance_conserved":True,
        "observed_interval_wallet_sender_gas_wei":
            audit["original_gas_balance_conservation"]["total_verified_sender_gas_wei"],
        "all_interval_wallet_sender_nonces_reconciled":True,
        "unaccounted_native_transfers":
            audit["unexpected_native_value_movements"],
        "full_owner_wallet_cost_basis_proven":False,
        "all_wallet_gas_spending_transactions_reconciled":False,
        "external_wallet_value_audit_complete":False,
        "fee_assessment_eligible":False,
        "hwm_updated":False,"fee_transferred":False,
    }
