"""Original booked net fees, Ethereum only, once-only Privy submission.

Payments require the independently audited, explicit new fee scope.
No existing HWM flag can enable it.
No client endpoint takes a fee amount, recipient or transaction.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException
from .storage import _db_url,_ensure_schema,_safe_did
from .ethereum_settlement_plan import COIN_NETWORKS,ETHEREUM_STABLES
from .shared_evm_fee_transfer_proof import owner_fee_treasury
from .shared_evm_execution import bound_evm_wallet_rpc
from .shared_evm_central_runtime import audit_original_evm_coin,original_evm_coin_record,_locked_original_evm_coin
from .shared_evm_consolidated_wallet_policy import NET_FEE_POLICY
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .eth_privy_p256_signer import sign_privy_rpc_payload
from .ethereum_settlement_status import _rpc_url,_num
from .shared_evm_swap_receipts import _receipt_rpc
from .ethereum_across_staged_journal import _lock_chain
from .ethereum_settlement_funding import _PROTECTED_FUNDING_SQL,summarize_pending_funding
from .realized_net_snapshot import NET_FILLS_SQL,hydrate_net_rows
from .realized_net_accounting import calculate_realized_net
from .net_fee_obligations import obligation_evidence,_legacy_fee_exists

LOG=logging.getLogger("mininex.net_fee_send")
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ADDR=re.compile(r"^0x[0-9a-f]{40}$")

def net_fee_worker_enabled():
    # The owner consent and live database are required; no separate per-coin
    # operator setup. The receipt worker remains passive even if sending stops.
    return bool(os.getenv("DATABASE_URL","").strip())

def fee_rule(record,*,asset,stable,now):
    scope=record["scope"]
    if (scope.get("net_fee_policy")!=NET_FEE_POLICY or scope["asset"]!=asset or scope["stable"]!=stable
            or scope["expires_at"]<=now or scope["treasury"]!=owner_fee_treasury(asset="ETH",stable=stable)):
        raise ValueError("Original explicit Ethereum fee consent missing")
    cap=int(scope["net_fee_atomic_cap"])
    # Candidate JSON is already independently verified and recomputed by
    # central audit; additionally bind the exact sole named rule here.
    name=f"MiniNex {asset} {stable} realized net fee Ethereum"
    rules=[r for r in record["candidate_json"]["rules"] if r.get("name")==name]
    if len(rules)!=1 or rules[0]["action"]!="ALLOW" or rules[0]["method"]!="eth_sendTransaction":
        raise ValueError("Original Ethereum fee policy rule missing")
    conditions=rules[0]["conditions"]
    expected={"chain_id":"1","to":ETHEREUM_STABLES[stable],"value":"0",
              "function_name":"transfer","transfer.to":scope["treasury"]}
    if any(not any(c.get("field")==k and c.get("operator")=="eq" and str(c.get("value")).lower()==v.lower()
                   for c in conditions) for k,v in expected.items()):
        raise ValueError("Original Ethereum fee token or recipient differs")
    if not any(c.get("field")=="transfer.value" and c.get("operator")=="lte" and str(c.get("value"))==str(cap)
               for c in conditions):
        raise ValueError("Original Ethereum fee cap differs")
    return cap

def fee_identity(obligation,record,*,now):
    asset=obligation["asset"];stable=obligation["stable"];wallet=record["wallet_address"]
    treasury=owner_fee_treasury(asset="ETH",stable=stable);amount=int(obligation["amount_atomic"])
    if (asset not in COIN_NETWORKS or stable not in ETHEREUM_STABLES or not _ADDR.fullmatch(wallet)
            or not _ADDR.fullmatch(treasury) or wallet==treasury or not 0<amount<=fee_rule(record,asset=asset,stable=stable,now=now)
            or obligation["evidence"]["coin_wallet"]!=wallet or obligation["evidence"]["fee_due_stable_atomic"]!=str(amount)
            or obligation["evidence"]["chain_id"]!=1):
        raise ValueError("Original net fee obligation differs from consent")
    token=ETHEREUM_STABLES[stable]
    tx={"from":wallet,"to":token,"chainId":1,"value":"0",
        "data":"0xa9059cbb"+format(int(treasury,16),"064x")+format(amount,"064x")}
    # Serializer uses ETH solely for its Ethereum chain registry; the
    # original accounting asset remains separately bound in the digest.
    url,body=bound_evm_wallet_rpc(asset="ETH",wallet_id=record["wallet_id"],wallet=wallet,tx=tx)
    digest=hashlib.sha256(json.dumps([obligation["privy_did"],asset,obligation["family"],obligation["fill_key"],
        obligation["evidence_sha256"],record["policy_sha256"],url,body],sort_keys=True,separators=(",",":")).encode("ascii")).hexdigest()
    return dict(url=url,body=body,tx=tx,wallet=wallet,treasury=treasury,token=token,amount=amount,
        digest=digest,idempotency_key="netfee_"+digest[:60])

async def preflight(client,identity):
    url=_rpc_url(1);tx=identity["tx"];wallet=identity["wallet"]
    async def rpc(method,params,rid):
        return await _receipt_rpc(client,url,method,params,rid)
    if _num(await rpc("eth_chainId",[],1))!=1:
        raise ValueError("Fee RPC must be Ethereum")
    balance=_num(await rpc("eth_call",[{"to":identity["token"],
        "data":"0x70a08231"+wallet[2:].rjust(64,"0")},"latest"],2))
    native=_num(await rpc("eth_getBalance",[wallet,"latest"],3))
    gas=_num(await rpc("eth_estimateGas",[{"from":wallet,"to":tx["to"],"value":"0x0","data":tx["data"]}],4))
    price=_num(await rpc("eth_gasPrice",[],5))
    if balance<identity["amount"] or gas<=0 or price<=0 or native<gas*price:
        raise HTTPException(409,"Original fee wallet lacks verified balance or gas")
    return balance

def claim_fee(did,asset,family,fill_key,record,identity,balance,*,now):
    if not net_fee_worker_enabled():
        raise HTTPException(409,"Net fee sending not enabled")
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(did,))
            if not cur.fetchone():raise ValueError("Original account missing")
            current=_locked_original_evm_coin(cur,did=did,asset=asset,stable=record["stable"],now=now)
            if any(current[k]!=record[k] for k in ("wallet_id","wallet_address","policy_id","policy_sha256","scope")):
                raise ValueError("Original remote consent changed")
            # A separate legacy fee revocation may never be bypassed.
            cur.execute("""SELECT 1 FROM mininex_evm_fee_auto_permissions
                WHERE privy_did=%s AND asset=%s AND (revoked_at IS NOT NULL OR status='REVOKED')""",(did,asset))
            if cur.fetchone():raise ValueError("Original fee permission revoked")
            cur.execute("""SELECT * FROM mininex_net_fee_obligations
                WHERE privy_did=%s AND asset=%s AND family=%s AND fill_key=%s FOR UPDATE""",(did,asset,family,fill_key))
            obligation=cur.fetchone()
            if not obligation or fee_identity(obligation,current,now=now)!=identity:
                raise ValueError("Original fee obligation changed")
            cur.execute("WITH fills AS ("+NET_FILLS_SQL+") SELECT * FROM fills WHERE asset=%(asset)s ORDER BY settled_at,fill_key",{"did":did,"asset":asset})
            valuations=calculate_realized_net(hydrate_net_rows(cur.fetchall()))
            row=next((r for r in valuations if r["family"]==family and r["fill_key"]==fill_key),None)
            proof=obligation_evidence(row) if row else None
            if not proof or proof[1]!=obligation["evidence_sha256"] or _legacy_fee_exists(cur,did,row):
                raise ValueError("Original fee accounting not independently matched")
            _lock_chain(cur,did,1)
            cur.execute(_PROTECTED_FUNDING_SQL,{"did":did,"stable":record["stable"],
                "excluded_native_asset":None,"excluded_native_key":None})
            # Only exact stable atomics matter for this fee; use actual
            # current stable reference to translate legacy USD reservations.
            rate=record["_fee_stable_usd"]
            pending=summarize_pending_funding(cur.fetchall(),asset=asset,stable=record["stable"],stable_usd=rate)
            if (int(pending["protected_fee_due_atomic"])<identity["amount"]
                    or balance<int(pending["pending_wallet_stable_atomic"])):

                raise HTTPException(409,"Original fee cannot consume reserved trading capital")
            cur.execute("""INSERT INTO mininex_net_fee_sends
                (privy_did,asset,family,fill_key,stable,wallet_id,wallet_address,treasury_wallet,
                 token_address,amount_atomic,policy_sha256,evidence_sha256,request_digest,idempotency_key,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SENDING')
                ON CONFLICT (privy_did,asset,family,fill_key) DO NOTHING RETURNING fill_key""",
                (did,asset,family,fill_key,record["stable"],record["wallet_id"],identity["wallet"],identity["treasury"],
                 identity["token"],identity["amount"],record["policy_sha256"],obligation["evidence_sha256"],
                 identity["digest"],identity["idempotency_key"]))
            if not cur.fetchone():raise HTTPException(409,"Original fee already claimed; never retry")

def finish_fee(did,asset,family,key,digest,tx_hash):
    if tx_hash is not None and (type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)):
        raise ValueError("Original fee hash invalid")
    db=_db_url()
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""UPDATE mininex_net_fee_sends SET status=%s,tx_hash=%s,updated_at=now()
                WHERE privy_did=%s AND asset=%s AND family=%s AND fill_key=%s AND request_digest=%s
                  AND status='SENDING' RETURNING fill_key""",
                ("SUBMITTED" if tx_hash else "RECONCILIATION_REQUIRED",tx_hash,did,asset,family,key,digest))
            if not cur.fetchone():raise ValueError("Original fee send journal changed")

async def submit_net_fee_once(client,obligation):
    if not net_fee_worker_enabled():
        return None
    did=_safe_did(obligation["privy_did"]);asset=obligation["asset"]
    audit=await audit_original_evm_coin(did,asset)
    record=await asyncio.to_thread(original_evm_coin_record,did,asset)
    if (audit.get("grant_source")!="PRIVY_CENTRAL_ONE_SIGNER"
            or audit.get("remote_verified_policy_sha256")!=record["policy_sha256"]
            or audit.get("remote_verified_wallet_id")!=record["wallet_id"]
            or record["stable"]!=obligation["stable"]):
        raise ValueError("Original independent Privy fee proof differs")
    now=int(time.time());identity=fee_identity(obligation,record,now=now)
    balance=await preflight(client,identity)
    from .shared_evm_policy_reference_rates import fetch_evm_policy_reference_rates,_price
    snapshot=await fetch_evm_policy_reference_rates(asset,record["stable"])
    record["_fee_stable_usd"]=_price(snapshot,record["stable"],int(time.time()))
    family,key=obligation["family"],obligation["fill_key"]
    await asyncio.to_thread(claim_fee,did,asset,family,key,record,identity,balance,now=int(time.time()))
    try:
        signature=sign_privy_rpc_payload(url=identity["url"],body=identity["body"],idempotency_key=identity["idempotency_key"])
        tx=await post_claimed_privy_evm_rpc(url=identity["url"],body=identity["body"],
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),authorization_signature=signature,
            idempotency_key=identity["idempotency_key"],client=client)
    except Exception:
        await asyncio.to_thread(finish_fee,did,asset,family,key,identity["digest"],None)
        raise
    await asyncio.to_thread(finish_fee,did,asset,family,key,identity["digest"],tx)
    return {"state":"SUBMITTED","tx_hash":tx,"paid_verified":False}

def pending_net_fee_sends():
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT o.* FROM mininex_net_fee_obligations o
                LEFT JOIN mininex_net_fee_sends s ON s.privy_did=o.privy_did AND s.asset=o.asset
                  AND s.family=o.family AND s.fill_key=o.fill_key
                WHERE s.fill_key IS NULL ORDER BY o.created_at,o.fill_key LIMIT 20""")
            return cur.fetchall()

async def reconcile_net_fee_sends_once(client):
    from .net_fee_receipts import reconcile_net_fee_receipts_once
    await reconcile_net_fee_receipts_once(client)
    if not net_fee_worker_enabled():
        return
    for obligation in await asyncio.to_thread(pending_net_fee_sends):
        try:
            await submit_net_fee_once(client,obligation)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Original net fee submission deferred or uncertain")
