"""Owner-driven per-coin EVM AUTO session state, no automatic sends.

Only a user-authenticated POST may start the session, and ONLY after fresh
remote Privy policy/signer/quorum audit and durable signed-owner intent.
The start endpoint does not send coins. Pause does not retry/cancel any
already committed blockchain transaction. Capital remains per-coin.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_remote_grant_audit import (
    get_owner_evm_remote_grant_audit,_read_grant_evidence_locked,
)
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.evm_live_auto_sessions")
_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","XRP","SOL","TRX"))


def _require_asset(asset:object)->str:
    if type(asset) is not str or asset not in _ASSETS:
        raise HTTPException(404,"Unsupported live EVM coin session")
    return asset


def _remote_ready(report:object,asset:str)->None:
    if (type(report) is not dict
            or report.get("mode")!="EVM_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT"
            or report.get("asset")!=asset
            or any(report.get(field) is not True for field in (
                "owner_wallet_current","exact_remote_policy_rules_verified",
                "privy_additional_signer_scoped","backend_quorum_current",
                "signed_coin_budget_intent_in_ledger"))):
        raise HTTPException(409,"Current approved Privy owner grant is missing")


def _start_locked(cur,*,did:str,asset:str,central:bool=False)->dict:
    """Lock independently verified original Privy grant and selected coin budget."""
    if central:
        from .shared_evm_central_runtime import _locked_original_evm_coin
        record=_locked_original_evm_coin(cur,did=did,asset=asset)
    else:
        record=_read_grant_evidence_locked(cur,did=did,asset=asset)
    if central and asset=="BTC":
        from .shared_evm_wallet_policy_prepare import _locked_selected_budget
        amount,revision=_locked_selected_budget(cur,did=did,asset=asset,
                             stable=record["stable"],expected_revision=record["budget_revision"])
        settings={"budget_usd":amount,"revision":revision,"stable":record["stable"]}
    elif central and asset=="ETH":
        from .shared_evm_central_eth_settings import locked_original_eth_settings
        settings=locked_original_eth_settings(
            cur,did=did,stable=record["stable"],
            revision=record["budget_revision"])
        cur.execute(
            "SELECT trading_paused FROM mininex_live_eth_limits "
            "WHERE privy_did=%s FOR UPDATE",(did,))
        paused=cur.fetchone()
        if type(paused) is not dict or paused.get("trading_paused") is not True:
            raise HTTPException(409,"Previous ETH trading engine is still active")
    else:
        cur.execute(
            """SELECT stable,revision,budget_usd
               FROM mininex_extra_coin_preferences
               WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
        settings=cur.fetchone()
    if central and record.get("scope",{}).get("settlement_policy")=="ETHEREUM_ACROSS_DIRECT_V1":
        from .ethereum_settlement_plan import settlement_route
        pair={"chain_id":settlement_route(asset,"BUY",record["stable"])["destination_chain_id"]}
    else:
        pair=_pair(asset,record["stable"],"BUY")
    if (type(settings) is not dict
            or settings.get("revision")!=record.get("budget_revision")
            or settings.get("stable")!=record["stable"]
            or type(settings.get("budget_usd")) not in (int,Decimal)
            or settings["budget_usd"]<=0
            or record.get("chain_id")!=pair["chain_id"]
            or record.get("wallet_address") is None):
        raise HTTPException(409,"User's coin budget differs from signed approval")
    cur.execute(
        """INSERT INTO mininex_live_evm_auto_sessions
           (privy_did,asset,chain_id,stable,wallet_address,
            budget_revision,status,started_by_owner_at)
           VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',now())
           ON CONFLICT(privy_did,asset) DO UPDATE SET
             chain_id=EXCLUDED.chain_id,stable=EXCLUDED.stable,
             wallet_address=EXCLUDED.wallet_address,
             budget_revision=EXCLUDED.budget_revision,
             status='ACTIVE',started_by_owner_at=now(),
             updated_at=now()
           WHERE mininex_live_evm_auto_sessions.status<>'EXIT_PENDING'
           RETURNING status""",
        (did,asset,pair["chain_id"],record["stable"],
         record["wallet_address"],settings["revision"]))
    saved=cur.fetchone()
    if type(saved) is not dict or saved.get("status")!="ACTIVE":
        raise HTTPException(409,"Concurrent EVM owner session conflict")
    return {
        "mode":"OWNER_EVM_LIVE_SESSION_CONTROL",
        "asset":asset,"chain_id":pair["chain_id"],
        "stable":record["stable"],"status":"ACTIVE",
        "started_by_owner":True,
        "worker_attached":False,"new_transactions_sent":0,
    }


def _stop_locked(cur,*,did:str,asset:str)->dict:
    cur.execute(
        """UPDATE mininex_live_evm_auto_sessions
           SET status='PAUSED',updated_at=now()
           WHERE privy_did=%s AND asset=%s AND (status='ACTIVE' OR status='EXIT_PENDING')
           RETURNING status""",(did,asset))
    row=cur.fetchone()
    return {
        "mode":"OWNER_EVM_LIVE_SESSION_CONTROL",
        "asset":asset,"status":"PAUSED","chain_id":None,
        "changed":type(row) is dict and row.get("status")=="PAUSED",
        "new_transactions_sent":0,
        "already_submitted_transactions_not_retried_or_cancelled":True,
    }


async def start_owner_evm_auto(
    did:str,asset:str,*,central:bool=False,
)->dict:
    did=_safe_did(did)
    asset=_require_asset(asset)
    if asset in ("BTC","XRP","SOL","TRX") and not central:
        raise HTTPException(409,"Original central wrapped trading grant required")
    # Must be independent live Privy REST readback, never a cached frontend
    # checkbox or an unverified 'policy ready' database string.
    report=(await get_owner_evm_remote_grant_audit(
        did,asset,central=True,
    ) if central else await get_owner_evm_remote_grant_audit(did,asset))
    _remote_ready(report,asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _start_locked(
                    cur,did=did,asset=asset,
                    **({"central":True} if central else {}),
                )
    except psycopg.Error:
        LOG.warning("Owner EVM live session startup persistence unavailable")
        raise HTTPException(503,"Cannot durably start EVM live session") from None


def pause_owner_evm_auto(did:str,asset:str)->dict:
    did=_safe_did(did)
    asset=_require_asset(asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _stop_locked(cur,did=did,asset=asset)
    except psycopg.Error:
        LOG.warning("Owner EVM live session pause unavailable")
        raise HTTPException(503,"Cannot durably pause EVM live session") from None


def request_owner_bridge_exit(did:str,asset:str)->dict:
    """Explicit authenticated Stop/Exit: prohibit new BUYs immediately.

    Cross-EVM bridge and Ethereum ERC20 sessions are eligible.
    AVAX remains pending until the authorized Stargate SELL settles.
    Does not broadcast, revoke consent, or mark an open position closed.
    """
    from .shared_evm_consolidated_wallet_policy import DIRECT_SETTLEMENT_ASSETS
    did=_safe_did(did)
    from .shared_evm_exit_inventory import ETHEREUM_EXIT_ASSETS,assert_central_eth_exit_ready_locked
    if asset not in DIRECT_SETTLEMENT_ASSETS | ETHEREUM_EXIT_ASSETS | {"AVAX"}:
        raise HTTPException(409,"Automatic exit route not connected for this coin")
    try:
        db=_db_url();_ensure_schema(db)
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(did,))
                if cur.fetchone() is None:
                    raise HTTPException(409,"Original owner unavailable")
                if asset=="ETH":
                    assert_central_eth_exit_ready_locked(cur,did)
                cur.execute("""UPDATE mininex_live_evm_auto_sessions
                    SET status='EXIT_PENDING',updated_at=now()
                    WHERE privy_did=%s AND asset=%s AND started_by_owner_at IS NOT NULL
                      AND status IN ('ACTIVE','PAUSED','EXIT_PENDING')
                    RETURNING chain_id,stable,status""",(did,asset))
                row=cur.fetchone()
                if not row:
                    raise HTTPException(409,"No original trade session available to exit")
                return {"mode":"OWNER_EVM_LIVE_SESSION_CONTROL","asset":asset,
                        "status":row["status"],"chain_id":row["chain_id"],"stable":row["stable"],
                        "new_transactions_sent":0}
    except psycopg.Error:
        raise HTTPException(503,"Cannot durably request exit") from None


def complete_bridge_exit_if_flat(did,asset,started):
    """Completion requires a locked, finalized strategy ledger with no pending
    bridge/approval/legacy work. Unrelated wallet balances are never sold.
    """
    from .ethereum_settlement_journal import pending_bridge_order
    from .ethereum_settlement_auto_plan import strategy_bridge_position_locked
    from .shared_evm_consolidated_wallet_policy import DIRECT_SETTLEMENT_ASSETS
    from .shared_evm_exit_inventory import ETHEREUM_EXIT_ASSETS,token_exit_position_locked,assert_central_eth_exit_ready_locked
    if asset not in DIRECT_SETTLEMENT_ASSETS | ETHEREUM_EXIT_ASSETS | {"AVAX"}:
        return False
    did=_safe_did(did)
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(did,))
            if cur.fetchone() is None:
                return False
            cur.execute("""SELECT status,started_by_owner_at,wallet_address
                FROM mininex_live_evm_auto_sessions WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
            session=cur.fetchone()
            if (not session or session["status"]!="EXIT_PENDING"
                    or session["started_by_owner_at"]!=started):
                return False
            if pending_bridge_order(cur,did,asset):
                return False
            for table in ("mininex_live_evm_router_approval_sends","mininex_live_evm_auto_broadcasts",
                          "mininex_live_evm_native_sell_intents","mininex_live_evm_native_auto_steps"):
                cur.execute(f"""SELECT 1 FROM {table} WHERE privy_did=%s AND asset=%s
                    AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,asset))
                if cur.fetchone() is not None:
                    return False
            cur.execute("""SELECT 1 FROM mininex_live_evm_budget_reservations
                WHERE privy_did=%s AND asset=%s AND state IN
                    ('RESERVED','SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,asset))
            if cur.fetchone() is not None:
                return False
            # Legacy local-chain buys require separate verified migration.
            # Do not call a bridge-only inventory of zero a completed exit
            # when the original local route may still own a position.
            cur.execute("""SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN destination_amount
                    ELSE -source_amount END),0) AS position
                FROM mininex_live_evm_verified_fills
                WHERE privy_did=%s AND asset=%s AND wallet_address=%s""",
                (did,asset,session["wallet_address"]))
            legacy=cur.fetchone()
            if not legacy or Decimal(legacy["position"])!=0:
                return False
            if asset=="ETH":
                assert_central_eth_exit_ready_locked(cur,did)
                cur.execute("""SELECT 1 FROM mininex_live_evm_native_sell_intents n
                    LEFT JOIN mininex_live_evm_completed_native_cycles c
                      ON c.privy_did=n.privy_did AND c.asset=n.asset AND c.intent_key=n.intent_key
                    WHERE n.privy_did=%s AND n.asset='ETH' AND n.status='FINALIZED_SUCCESS'
                      AND c.intent_key IS NULL LIMIT 1""",(did,))
                if cur.fetchone() is not None:
                    return False
            if asset in ETHEREUM_EXIT_ASSETS:
                if token_exit_position_locked(cur,did,asset,session["wallet_address"])!=0:
                    return False
            elif strategy_bridge_position_locked(cur,did,asset,session["wallet_address"])!=0:
                return False
            cur.execute("""UPDATE mininex_live_evm_auto_sessions SET status='STOPPED',updated_at=now()
                WHERE privy_did=%s AND asset=%s AND status='EXIT_PENDING' RETURNING status""",(did,asset))
            return cur.fetchone() is not None


def get_owner_evm_auto(did:str,asset:str)->dict:
    did=_safe_did(did)
    asset=_require_asset(asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT status,chain_id,stable,budget_revision
                       FROM mininex_live_evm_auto_sessions
                       WHERE privy_did=%s AND asset=%s""",(did,asset))
                row=cur.fetchone()
                # Only original Privy transaction hashes recorded in the
                # durable swap journal count as genuine blockchain sends.
                # Never claim an actual trade merely from ACTIVE session state.
                sent=0
                finalized=0
                worker_seen=False
                if type(row) is dict:
                    cur.execute(
                        """WITH selected AS (
                               SELECT %s::text AS did,%s::text AS asset
                           ), original_swaps AS (
                               SELECT b.chain_id,b.tx_hash,
                                   b.status='FINALIZED_SUCCESS' AS finalized
                               FROM mininex_live_evm_auto_broadcasts b
                               JOIN selected k ON k.did=b.privy_did AND k.asset=b.asset
                               WHERE b.tx_hash IS NOT NULL
                                 AND b.status IN ('SUBMITTED','FINALIZED_SUCCESS',
                                                  'FINALIZED_REVERTED','RECONCILIATION_REQUIRED')
                               UNION ALL
                               SELECT s.source_chain_id,s.source_tx_hash,
                                   COALESCE(f.outcome='FINALIZED',FALSE) AS finalized
                               FROM mininex_ethereum_settlement_submissions s
                               JOIN mininex_ethereum_settlement_quotes q
                                 ON q.quote_id=s.quote_id AND q.privy_did=s.privy_did
                               JOIN selected k ON k.did=q.privy_did AND k.asset=q.asset
                               LEFT JOIN mininex_ethereum_settlement_finality f
                                 ON f.quote_id=s.quote_id AND f.privy_did=s.privy_did
                                  AND f.source_tx_hash=s.source_tx_hash
                               WHERE s.state='SUBMITTED' AND s.source_tx_hash IS NOT NULL
                           ), unique_swaps AS (
                               SELECT chain_id,tx_hash,bool_or(finalized) AS finalized
                               FROM original_swaps GROUP BY chain_id,tx_hash
                           )
                           SELECT COUNT(*) AS sent,
                               COUNT(*) FILTER (WHERE finalized) AS finalized
                           FROM unique_swaps""",
                        (did,asset))
                    counted=cur.fetchone()
                    if type(counted) is dict:
                        sent=int(counted.get("sent") or 0)
                        finalized=int(counted.get("finalized") or 0)
                    cur.execute(
                        """SELECT last_poll_at FROM mininex_live_worker_heartbeats
                           WHERE worker_name='shared_evm_auto'
                             AND status='RUNNING'
                             AND last_poll_at>now()-INTERVAL '90 seconds'""")
                    heartbeat=cur.fetchone()
                    worker_seen=(type(heartbeat) is dict and
                                 heartbeat.get("last_poll_at") is not None)
    except psycopg.Error:
        raise HTTPException(503,"EVM live session status unavailable") from None
    return {
        "mode":"OWNER_EVM_LIVE_SESSION_CONTROL",
        "asset":asset,"status":row["status"] if type(row) is dict else "NOT_STARTED",
        "chain_id":row.get("chain_id") if type(row) is dict else None,
        "stable":row.get("stable") if type(row) is dict else None,
        "worker_attached":worker_seen,"new_transactions_sent":0,
        "original_blockchain_swaps_sent":sent,
        "original_blockchain_swaps_finalized":finalized,
    }
