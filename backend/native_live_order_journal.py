"""Account-scoped native BTC/SOL/XRP original-order journal for future LIVE.

No public FastAPI order route, no Privy signer, and no broadcast in this file.
A strategy event has a stable idempotency key across worker restarts.
The first SENDING claim is irrevocable: UNKNOWN is never a send retry.
The saved immutable identity binds the real owner, *both* blockchain wallets,
source network, consent, Privy policy, quote and budget revision.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from .storage import _db_url, _ensure_schema, _safe_did
from .native_coin_balances import _valid_address
from .blockchain_preview import BTC_RE, ETH_RE
from .native_trade_receipts import _valid_native_hash
from .manual_swap_live_quotes import THOR_ASSETS

_NATIVE_NETWORK={"BTC":"bitcoin-mainnet","SOL":"solana-mainnet",
                 "XRP":"xrpl-mainnet","TRX":"tron-mainnet",
                 "DOGE":"dogecoin-mainnet"}
_BUY_ONLY=frozenset(("TRX","DOGE"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_EVENT=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_MONEY=re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_FINAL=frozenset(("DESTINATION_FINALIZED","REFUNDED_VERIFIED","CANCELLED"))
_PENDING=frozenset(("SENDING","SUBMITTED","RECONCILIATION_REQUIRED",
                   "SOURCE_FINALIZED"))


def _positive_amount(amount:object,decimals:int)->str:
    if type(amount) is not str or len(amount)>52 or not _MONEY.fullmatch(amount):
        raise ValueError("Invalid exact native order amount")
    fraction=amount.partition(".")[2]
    if len(fraction)>decimals:
        raise ValueError("Native source precision mismatch")
    try:
        n=Decimal(amount)
    except InvalidOperation:
        raise ValueError("Invalid exact native order amount") from None
    if not n.is_finite() or n<=0:
        raise ValueError("Invalid exact native order amount")
    return format(n,"f")


def _wallet_matches(network:str,address:object)->bool:
    if type(address) is not str:
        return False
    if network=="ethereum-mainnet":
        return bool(ETH_RE.fullmatch(address))
    if network=="bitcoin-mainnet":
        return bool(BTC_RE.fullmatch(address))
    asset={"solana-mainnet":"SOL","xrpl-mainnet":"XRP",
           "tron-mainnet":"TRX","dogecoin-mainnet":"DOGE"}.get(network)
    return bool(asset and _valid_address(asset,address))


def _valid_source_hash(asset:str,side:str,hash_:object)->bool:
    if type(hash_) is not str:
        return False
    if side=="BUY":
        return bool(re.fullmatch(r"0x[0-9a-f]{64}",hash_))
    if asset=="BTC":
        return bool(re.fullmatch(r"[0-9a-f]{64}",hash_))
    return _valid_native_hash(asset,hash_)


def immutable_native_order_identity(
    *,did:str,asset:str,side:str,stable:str,source_wallet:str,
    destination_wallet:str,amount:str,quote:dict,quote_expires_at:datetime,
    strategy_run_id:str,signal_event_id:str,budget_revision:int,
    owner_consent_sha256:str,privy_grant_sha256:str,
)->dict:
    """Prove precise proposed terms, NOT user signing authority.

    The quote must come from a separately validated executable THORNode
    destination-bound response; the public MiniNex indicative MCP quote
    NEVER satisfies this gate. No existing test/preview can reach a send.
    """
    did=_safe_did(did)
    if (asset not in _NATIVE_NETWORK or side not in ("BUY","SELL")
            or (side=="SELL" and asset in _BUY_ONLY)
            or stable not in ("USDC","USDT")
            or (asset=="XRP" and stable!="USDC")
            or type(budget_revision) is not int or budget_revision<0
            or type(strategy_run_id) is not str
            or not _EVENT.fullmatch(strategy_run_id)
            or type(signal_event_id) is not str
            or not _EVENT.fullmatch(signal_event_id)
            or type(owner_consent_sha256) is not str
            or not _SHA.fullmatch(owner_consent_sha256)
            or type(privy_grant_sha256) is not str
            or not _SHA.fullmatch(privy_grant_sha256)):
        raise ValueError("Native grant or strategy identity invalid")
    network=_NATIVE_NETWORK[asset]
    chain="ethereum-mainnet" if side=="BUY" else network
    dest_chain=network if side=="BUY" else "ethereum-mainnet"
    if (not _wallet_matches(chain,source_wallet)
            or not _wallet_matches(dest_chain,destination_wallet)):
        raise ValueError("Native wallet / settlement network mismatch")
    amount=_positive_amount(amount,6 if side=="BUY" else
                            (8 if asset=="BTC" else 9 if asset=="SOL" else 6))
    if (not isinstance(quote_expires_at,datetime)
            or quote_expires_at.tzinfo is None
            or quote_expires_at<=datetime.now(timezone.utc)):
        raise ValueError("Executable native quote expired")
    # The real private THORNode adapter supplies an explicitly READ-ONLY,
    # independently destination/vault/refund checked route. Persist its exact
    # immutable evidence; NEVER infer from this that a wallet may be signed.
    if (type(quote) is not dict
            or quote.get("mode")!="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or quote.get("from_network")!=chain
            or quote.get("to_network")!=dest_chain
            or quote.get("source_asset")!=(stable if side=="BUY" else asset)
            or quote.get("destination_asset")!=(asset if side=="BUY" else stable)
            or quote.get("amount")!=amount
            or quote.get("source_wallet")!=source_wallet
            or quote.get("destination_wallet")!=destination_wallet
            or quote.get("quote_expires_at")!=quote_expires_at
            or type(quote.get("inbound_vault")) is not str
            or not _wallet_matches(chain,quote["inbound_vault"])
            or (quote["inbound_vault"].lower()==source_wallet.lower()
                if chain=="ethereum-mainnet" else
                quote["inbound_vault"]==source_wallet)
            or (side=="BUY" and (
                not _wallet_matches("ethereum-mainnet",quote.get("inbound_router"))
                or quote.get("needs_erc20_router") is not True))
            or (side=="SELL" and quote.get("inbound_router") is not None)
            or quote.get("original_vault_verified") is not True
            or quote.get("required_chain_memo_verified") is not True
            or quote.get("refund_path_verified") is not True
            or quote.get("exact_venue_fees_verified") is not True
            or type(quote.get("expected_output_units_1e8")) is not str
            or not re.fullmatch(r"[1-9][0-9]*",quote["expected_output_units_1e8"])
            or type(quote.get("venue_fee_units_1e8")) is not str
            or not re.fullmatch(r"[0-9]+",quote["venue_fee_units_1e8"])
            or quote.get("transaction_encoding_verified") is not False
            or quote.get("privy_native_signer_verified") is not False
            or quote.get("source_transaction_sent") is not False
            or quote.get("execution_enabled") is not False
            or quote.get("can_submit") is not False):
        raise ValueError("Private native quote is not owner-bound or current")
    memo=quote.get("memo")
    if (type(memo) is not str or not 6<len(memo.encode("utf-8"))<=250
            or any(c in memo for c in "\r\n\x00")):
        raise ValueError("Original source refund memo unverified")
    parts=memo.split(":")
    owners=parts[2].split("/") if len(parts)>=3 else []
    expected_target=THOR_ASSETS[asset if side=="BUY" else stable][0]
    if (len(parts)<3 or parts[0].upper() not in ("=","S","SWAP")
            or parts[1].upper()!=expected_target.upper()
            or len(owners)!=2
            or (owners[0].lower()!=destination_wallet.lower()
                if side=="SELL" else owners[0]!=destination_wallet)
            or (owners[1].lower()!=source_wallet.lower()
                if side=="BUY" else owners[1]!=source_wallet)):
        raise ValueError("Original destination or refund account redirected")
    # Canonicalize datetimes: the actual THORChain quote has a UTC expiry.
    quote_payload={key:(quote[key].isoformat()
                        if isinstance(quote[key],datetime) else quote[key])
                   for key in sorted(quote)}
    quote_digest=hashlib.sha256(json.dumps(
        quote_payload,sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    signal_digest=hashlib.sha256(json.dumps(
        [did,asset,strategy_run_id,signal_event_id],
        separators=(",",":"),
    ).encode("utf-8")).hexdigest()
    key="native_"+signal_digest
    identity={
        "privy_did":did,"asset":asset,"intent_key":key,
        "chain":chain,"destination_chain":dest_chain,
        "side":side,"stable":stable,"budget_revision":budget_revision,
        "source_wallet":source_wallet,"destination_wallet":destination_wallet,
        "input_amount":amount,"immutable_quote_sha256":quote_digest,
        "owner_consent_sha256":owner_consent_sha256,
        "privy_grant_sha256":privy_grant_sha256,
        "quote_expires_at":quote_expires_at,
    }
    # No plaintext memo, ephemeral vault address, private key or signer
    # authorization token is written to the database or API.
    digest_fields={k:(v.isoformat() if isinstance(v,datetime) else v)
                   for k,v in identity.items()}
    identity["payload_sha256"]=hashlib.sha256(json.dumps(
        digest_fields,sort_keys=True,separators=(",",":"),
    ).encode("utf-8")).hexdigest()
    return identity


def _persist_locked(cur,identity:dict)->str:
    """Persist first quote identity; same signal with changed terms is denied."""
    did=identity["privy_did"]
    asset=identity["asset"]
    key=identity["intent_key"]
    cur.execute(
        "SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",
        (did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"Unverified native order owner account")
    cur.execute(
        """SELECT payload_sha256,status
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",(did,asset,key))
    old=cur.fetchone()
    if old is not None:
        if old.get("payload_sha256")!=identity["payload_sha256"]:
            raise HTTPException(409,"Native signal identity conflicts with original quote")
        return old["status"]
    cur.execute(
        """INSERT INTO mininex_live_native_order_intents
           (privy_did,asset,intent_key,chain,side,stable,budget_revision,
            source_wallet,destination_wallet,input_amount,
            immutable_quote_sha256,owner_consent_sha256,privy_grant_sha256,
            payload_sha256,quote_expires_at,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PREPARED')
           RETURNING status""",
        tuple(identity[field] for field in (
            "privy_did","asset","intent_key","chain","side","stable",
            "budget_revision","source_wallet","destination_wallet",
            "input_amount","immutable_quote_sha256","owner_consent_sha256",
            "privy_grant_sha256","payload_sha256","quote_expires_at")))
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!="PREPARED":
        raise HTTPException(409,"Native original order could not be persisted")
    return "PREPARED"


def _claim_once_locked(cur,*,did:str,asset:str,key:str,digest:str,
                       permission_audit:dict)->str:
    """Called only by a future fully signed native executor, never public API.

    Always perform actual Privy remote readback before passing the evidence.
    The journal protects atomicity; it does not *create* Privy permission.
    """
    if (type(permission_audit) is not dict
            or permission_audit.get("owner_wallet_current") is not True
            or permission_audit.get("exact_remote_policy_verified") is not True
            or permission_audit.get("backend_quorum_current") is not True
            or permission_audit.get("owner_signed_budget_current") is not True
            or permission_audit.get("did")!=did
            or permission_audit.get("asset")!=asset
            or permission_audit.get("payload_sha256")!=digest):
        raise HTTPException(409,"Real native signer grant missing or stale")
    cur.execute(
        "SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",
        (did,))
    if cur.fetchone()!={"privy_did":did}:
        raise HTTPException(409,"Native owner not durably verified")
    cur.execute(
        """SELECT chain,source_wallet,quote_expires_at,status,payload_sha256,
                  owner_consent_sha256,privy_grant_sha256,budget_revision
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",(did,asset,key))
    original=cur.fetchone()
    if (type(original) is not dict or original.get("status")!="PREPARED"
            or original.get("payload_sha256")!=digest
            or original.get("owner_consent_sha256")!=
               permission_audit.get("owner_consent_sha256")
            or original.get("privy_grant_sha256")!=
               permission_audit.get("privy_grant_sha256")
            or original.get("budget_revision")!=
               permission_audit.get("budget_revision")
            or original.get("source_wallet")!=
               permission_audit.get("source_wallet")):
        raise HTTPException(409,"Native original send already claimed or changed")
    # Source-chain finalization means the original transaction is mined
    # and its source nonce/UTXO can no longer be reused. The cross-chain
    # destination may still take a long time, but should not freeze
    # unrelated user-authorized BUY/SELL signals or other per-coin budgets.
    # Only an unconfirmed or ambiguous source SEND serializes this wallet.
    # Actual pending capital is still checked against user settings by the
    # live order coordinator, not through an artificial one-trade gate.
    cur.execute(
        """SELECT COUNT(*) AS pending
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND chain=%s
             AND status IN ('SENDING','SUBMITTED',
                            'RECONCILIATION_REQUIRED')""",
        (did,original["chain"]))
    pending=cur.fetchone()
    if (type(pending) is not dict or type(pending.get("pending")) is not int
            or pending["pending"]!=0):
        raise HTTPException(409,"Another original wallet send is unresolved")
    if original["chain"]=="ethereum-mainnet":
        # The original BTC/SOL/XRP BUY uses this owner's ETH wallet nonce.
        # Lock already held: never race ETH, LINK/UNI, fee sends or approvals.
        # No capital, turnover or trade-frequency limits are introduced.
        cur.execute(
            """SELECT (SELECT COUNT(*) FROM mininex_live_eth_auto_broadcasts
                WHERE privy_did=%s AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
              + (SELECT COUNT(*) FROM mininex_live_eth_auto_native_steps
                WHERE privy_did=%s AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
              + (SELECT COUNT(*) FROM mininex_live_evm_auto_broadcasts
                WHERE privy_did=%s AND chain_id=1 AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
              + (SELECT COUNT(*) FROM mininex_evm_auto_fee_sends
                WHERE privy_did=%s AND chain_id=1 AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
              + (SELECT COUNT(*) FROM mininex_live_evm_router_approval_sends
                WHERE privy_did=%s AND chain_id=1 AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
              AS pending""",(did,did,did,did,did))
        shared=cur.fetchone()
        if (type(shared) is not dict or type(shared.get("pending")) is not int
                or shared["pending"]!=0):
            raise HTTPException(409,"Existing Ethereum owner wallet send unresolved")
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET status='SENDING',updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='PREPARED' AND payload_sha256=%s
             AND quote_expires_at>now() RETURNING status""",
        (did,asset,key,digest))
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!="SENDING":
        raise HTTPException(409,"Original native quote expired or claimed")
    return "SENDING"


def _record_send_result_locked(
    cur,*,did:str,asset:str,key:str,digest:str,tx_hash:str|None,
)->str:
    """Unknown network outcome is permanently non-retriable until reconciled."""
    if tx_hash is not None:
        if type(asset) is not str or type(tx_hash) is not str:
            raise ValueError("Invalid original source transaction id")
    cur.execute(
        """SELECT status,side,payload_sha256,source_tx_hash
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",(did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("status")!="SENDING"
            or row.get("payload_sha256")!=digest
            or row.get("source_tx_hash") is not None):
        raise HTTPException(409,"Original native send is immutable")
    if tx_hash is not None and not _valid_source_hash(asset,row["side"],tx_hash):
        raise ValueError("Source chain transaction hash mismatch")
    state="SUBMITTED" if tx_hash is not None else "RECONCILIATION_REQUIRED"
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET status=%s,source_tx_hash=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='SENDING' AND payload_sha256=%s
           RETURNING status""",
        (state,tx_hash,did,asset,key,digest))
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!=state:
        raise HTTPException(409,"Cannot persist original native send outcome")
    return state


def _claim_signed_native_source_once_locked(
    cur,*,did:str,asset:str,key:str,digest:str,
    signed_source_tx_hash:str,permission_audit:dict,
)->str:
    """Durably save the exact signed tx hash in SAME transaction as SENDING.

    Must be called only after verifying the full original signed wire and
    independent real Privy policy. A committed SENDING row is never retried
    even when no response is received from the public source network.
    """
    if (asset not in _NATIVE_NETWORK
            or not _valid_source_hash(asset,"SELL",signed_source_tx_hash)
            or type(key) is not str
            or not re.fullmatch(r"native_[0-9a-f]{64}",key)):
        raise ValueError("Original signed native transaction ID invalid")
    state=_claim_once_locked(
        cur,did=did,asset=asset,key=key,digest=digest,
        permission_audit=permission_audit,
    )
    if state!="SENDING":
        raise HTTPException(409,"Original native source not atomically claimed")
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET source_signed_tx_hash=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='SENDING' AND side='SELL'
             AND source_signed_tx_hash IS NULL
             AND source_tx_hash IS NULL AND payload_sha256=%s
           RETURNING source_signed_tx_hash""",
        (signed_source_tx_hash,did,asset,key,digest),
    )
    row=cur.fetchone()
    if (type(row) is not dict
            or row.get("source_signed_tx_hash")!=signed_source_tx_hash):
        raise HTTPException(409,"Original signed transaction was not reserved")
    return state


def _record_signed_native_source_result_locked(
    cur,*,did:str,asset:str,key:str,digest:str,
    signed_source_tx_hash:str,network_accepted:bool,
)->str:
    """Record the one network answer or permanently unknown response.

    A timeout/unknown RPC response remains RECONCILIATION_REQUIRED with the
    expected source hash stored for read-only chain lookup; NEVER resubmit.
    """
    if (type(network_accepted) is not bool
            or not _valid_source_hash(asset,"SELL",signed_source_tx_hash)):
        raise ValueError("Invalid original native network observation")
    cur.execute(
        """SELECT side,status,payload_sha256,source_signed_tx_hash
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",(did,asset,key))
    current=cur.fetchone()
    if (type(current) is not dict or current.get("side")!="SELL"
            or current.get("status")!="SENDING"
            or current.get("payload_sha256")!=digest
            or current.get("source_signed_tx_hash")!=signed_source_tx_hash):
        raise HTTPException(409,"Original signed source ID no longer reserved")
    return _record_send_result_locked(
        cur,did=did,asset=asset,key=key,digest=digest,
        tx_hash=signed_source_tx_hash if network_accepted else None,
    )


def persist_internal_native_order(identity:dict)->dict:
    """Internal-only future executor entry; no signer, approval or broadcast."""
    if (type(identity) is not dict or identity.get("asset") not in _NATIVE_NETWORK
            or identity.get("privy_did")!=_safe_did(identity.get("privy_did"))
            or type(identity.get("payload_sha256")) is not str
            or not _SHA.fullmatch(identity["payload_sha256"])):
        raise ValueError("Invalid original native journal identity")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            state=_persist_locked(cur,identity)
    return {"asset":identity["asset"],"intent_key":identity["intent_key"],
            "state":state,"signed":False,"send_attempted":False,
            "retry_allowed":False}
