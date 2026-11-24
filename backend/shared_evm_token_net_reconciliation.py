"""Real owner-only LINK/UNI USD FIFO after complete onchain token audit.

Actual persisted MiniNex fill rows are reread from Postgres, bound to their
original finalized Privy wallet swaps, mined block times and historical prices.
All ERC20 wallet token in/out events and archived balances must match exactly.
Only then can the FIFO math be evaluated; this reader does NOT assess fees.

This is intentionally limited to LINK/UNI token trades: native BNB/POL/AVAX
need an additional *native wallet* custody audit beyond their wrap/unwrap
receipts. Missing old historical prices fail closed, never use today quotes.
"""
from __future__ import annotations

import asyncio

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_fifo_net import reconcile_verified_evm_fifo_net
from .shared_evm_fill_history import _verified_fill_row
from .shared_evm_historical_fill_bridge import independently_price_stored_evm_fill
from .shared_evm_wallet_token_audit import audit_owner_erc20_token_inventory
from .storage import _db_url, _ensure_schema, _safe_did
from .shared_evm_approval_costs import attach_original_approval_costs

_ASSETS = frozenset(("LINK", "QNT", "UNI", "ARB"))


def _verified_account_fill(row: dict, asset: str) -> dict:
    if (type(row) is not dict
            or row.get("broadcast_status") != "FINALIZED_SUCCESS"
            or row.get("broadcast_tx_hash") != row.get("tx_hash")
            or row.get("broadcast_side") != row.get("side")
            or row.get("broadcast_stable") != row.get("stable")
            or row.get("broadcast_wallet_address") != row.get("wallet_address")):
        raise ValueError("Durable original order and completed actual fill differ")
    return _verified_fill_row(row,asset)


def _load_all_verified_owner_token_fills(did: str, asset: str) -> list[dict]:
    """Stream all trades, not the UI's 30/50 trade display window."""
    did = _safe_did(did)
    if asset not in _ASSETS:
        raise ValueError("Only ERC20 token custody can be audited here")
    try:
        db = _db_url()
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
                       ORDER BY f.created_at ASC,f.intent_key ASC""",
                    (did,asset))
                fills = []
                while True:
                    rows = cur.fetchmany(200)
                    if not rows:
                        break
                    fills.extend(_verified_account_fill(row,asset) for row in rows)
        return fills
    except psycopg.Error:
        raise HTTPException(503,"Full EVM owner fill provenance unavailable") from None


async def read_owner_erc20_verified_realized_net(
    *,did: str,asset: str,client: httpx.AsyncClient,
    now: int | None = None,
) -> dict:
    """One verified complete history; no client prices, fills or audit flags."""
    did = _safe_did(did)
    if asset not in _ASSETS or type(client) is not httpx.AsyncClient:
        raise ValueError("LINK/UNI owner history and internal HTTP client required")
    stored = await asyncio.to_thread(
        _load_all_verified_owner_token_fills,did,asset)
    if not stored:
        return {
            "mode":"RECONCILED_EVM_FIFO_NET_READ_ONLY",
            "asset":asset,"closed_trades":[],
            "history_complete":True,"hwm_updated":False,"fee_assessed":False,
        }
    wallet = stored[0]["wallet_address"]
    if any(f["wallet_address"] != wallet for f in stored):
        raise ValueError("Multiple owner wallets cannot share unknown FIFO basis")
    priced = []
    for row in stored:
        proof = await independently_price_stored_evm_fill(
            did=did,row=row,client=client,now=now,durable_rates=True)
        if (proof.get("historic_usd_priced") is not True
                or proof.get("native_cycle_complete") is not True):
            raise ValueError("Source execution costs are not proven")
        priced.append(proof)
    # Trade history is in actual mined order, not the arbitrary time the
    # worker happened to insert a finalized result in Postgres.
    priced.sort(key=lambda f:(f["block_number"],f["transaction_index"]))
    audit = await audit_owner_erc20_token_inventory(
        asset=asset,wallet=wallet,fills=priced,client=client)
    if (audit.get("external_inventory_reconciled") is not True
            or audit.get("all_asset_movements_match_verified_orders") is not True
            or audit.get("matched_mininex_swap_transfers") != len(priced)):
        raise ValueError("Unaccounted wallet trades or transfers")
    # These assets use only a single exact-input ERC20 Router02 call with
    # native tx.value=0. check_user_evm_swap proves actual in/out transfers
    # and receipt-gas cost for each, so there is no extra native conversion.
    if any(f.get("native_conversion") is not None for f in priced):
        raise ValueError("Unexpected ERC20-token native conversion cost")
    # Include independently finalized approvals and resets in FIFO cost.
    approval_report=await attach_original_approval_costs(
        did=did,asset=asset,wallet=wallet,fills=priced,client=client,now=now)
    audit["no_other_execution_costs_verified"] = True
    result = reconcile_verified_evm_fifo_net({
        "mode":"SERVER_VERIFIED_EVM_CUSTODY_HISTORY",
        "asset":asset,"wallet_address":wallet,
        "wallet_ownership_verified":True,
        "history_not_truncated":True,"contains_paper":False,
        "asset_transfer_audit":audit,"fills":priced,
    })
    result["asset_transfer_audit"] = audit
    result["approval_costs"] = approval_report
    result["hwm_updated"] = False
    result["fee_assessed"] = False
    result["auto_fee_transfer_enabled"] = False
    return result


async def reconcile_owner_erc20_hwm_after_verified_sell(
    *,did: str,asset: str,client: httpx.AsyncClient,
    now: int | None = None,
) -> dict:
    """Internal accounting only: provenance -> FIFO net -> idempotent HWM.

    The same immutable closing SELL hash is checked again under database
    locks by the existing five-coin HWM engine. No Privy fee signer or
    treasury transfer is authorized by updating the HWM.
    """
    if asset not in _ASSETS:
        raise ValueError("Native chain custody still requires separate audit")
    report = await read_owner_erc20_verified_realized_net(
        did=did,asset=asset,client=client,now=now)
    if (report.get("mode")!="RECONCILED_EVM_FIFO_NET_READ_ONLY"
            or report.get("asset")!=asset or report.get("fee_assessed") is not False
            or report.get("hwm_updated") is not False):
        raise ValueError("Unverified original FIFO net report")
    closes = report.get("closed_trades")
    if type(closes) is not list:
        raise ValueError("No actual closed trade evidence")
    if closes:
        audit = report.get("asset_transfer_audit")
        if (type(audit) is not dict
                or audit.get("external_inventory_reconciled") is not True
                or audit.get("no_other_execution_costs_verified") is not True
                or report.get("wallet_address") != audit.get("wallet_address")):
            raise ValueError("No complete on-chain token custody proof")
    from .shared_evm_hwm_ledger import record_internal_evm_realized_close
    results=[]
    for close in closes:
        if (type(close) is not dict
                or close.get("mode") != "RECONCILED_EVM_NET_USD_MATH_ONLY"
                or close.get("asset") != asset
                or close.get("wallet_address") != report.get("wallet_address")
                or close.get("hwm_updated") is not False
                or close.get("fee_assessed") is not False
                or type(close.get("realized_net_usd")) is not str):
            raise ValueError("Original verified closing event changed")
        # One transaction per SELL in mined chronological order. The
        # accounting ledger is idempotent; no historical close is skipped.
        results.append(await asyncio.to_thread(
            record_internal_evm_realized_close,
            did=did,asset=asset,intent_key=close["intent_key"],
            source_tx_hash=close["tx_hash"],
            realized_net_usd=close["realized_net_usd"]))
    return {
        "mode":"HWM_ACCOUNTED_VERIFIED_ERC20_SELLS",
        "asset":asset,"verified_closes":len(closes),
        "recorded_events":results,"fee_transferred":False,
        "privy_treasury_signer_used":False,
    }
