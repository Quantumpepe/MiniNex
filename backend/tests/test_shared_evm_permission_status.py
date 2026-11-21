"""Durable, independent user/coin Privy grant status never enables trading."""
from datetime import datetime,timedelta,timezone
import inspect

import pytest
from fastapi import HTTPException

from backend.shared_evm_permission_status import (
    assess_evm_permission_record,read_owner_evm_permission_status,
)
from backend.storage import SCHEMA

NOW=datetime(2026,11,17,13,tzinfo=timezone.utc)
DID="did:privy:test"
SHA="a"*64
CHAIN={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1,"ARB":42161}


def stored(asset="BNB",state="GRANT_VERIFIED"):
    return {
        "asset":asset,"chain_id":CHAIN[asset],
        "stable":"USDT" if asset=="BNB" else "USDC",
        "wallet_id":"wallet_123456","wallet_address":"0x"+"ab"*20,
        "owner_id":"owner_123456","signer_id":"signer_123456",
        "policy_id":"policy_123456","policy_sha256":SHA,
        "remote_policy_sha256":SHA,"consent_sha256":SHA,
        "consent_verified_at":NOW-timedelta(minutes=5),
        "grant_verified_at":NOW-timedelta(minutes=3),
        "revoked_at":None,
        "expires_at":NOW+timedelta(hours=1),
        "status":state,
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_each_coin_needs_independent_privy_permission_record(asset):
    r=assess_evm_permission_record(stored(asset),asset=asset,now=NOW)
    assert r["asset"]==asset
    assert r["chain_id"]==CHAIN[asset]
    assert r["user_consent_recorded"] is True
    assert r["server_policy_digest_matched"] is True
    assert r["signer_grant_recorded"] is True
    assert r["privy_live_readback_completed"] is False
    assert r["live_worker_permission_verified"] is False
    assert r["auto_execution_enabled"] is False
    assert r["can_submit"] is False


def test_no_grant_record_is_inert():
    r=assess_evm_permission_record(None,asset="LINK",now=NOW)
    assert r["status"]=="NOT_PREPARED"
    assert r["can_submit"] is False


@pytest.mark.parametrize("state",["DRAFT","CONSENT_VERIFIED","REVOKED"])
def test_non_granted_permissions_never_claim_active_signer(state):
    row=stored("AVAX",state=state)
    if state=="REVOKED":row["revoked_at"]=NOW
    r=assess_evm_permission_record(row,asset="AVAX",now=NOW)
    assert r["signer_grant_recorded"] is False


@pytest.mark.parametrize("field,value",[
    ("chain_id",1),("stable","DAI"),
    ("wallet_address","0x"+"ab"*19),
    ("wallet_id","bad"),("owner_id","signer_123456"),
    ("policy_sha256","z"*64),("status","ENABLED"),
])
def test_forged_or_wrong_chain_policy_record_never_claims_consent(field,value):
    row=stored("BNB")
    row[field]=value
    with pytest.raises(ValueError):
        assess_evm_permission_record(row,asset="BNB",now=NOW)


def test_mismatched_digest_or_expiry_never_activates():
    row=stored("UNI")
    row["remote_policy_sha256"]="b"*64
    with pytest.raises(ValueError):
        assess_evm_permission_record(row,asset="UNI",now=NOW)
    row=stored("UNI")
    row["expires_at"]=NOW-timedelta(seconds=1)
    r=assess_evm_permission_record(row,asset="UNI",now=NOW)
    assert r["policy_current"] is False
    assert r["signer_grant_recorded"] is False


def test_permission_schema_and_get_only_auth_endpoint():
    schema="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_live_evm_owner_permissions" in schema
    assert "PRIMARY KEY(privy_did,asset)" in schema
    assert "owner_id <> signer_id" in schema
    from backend.app import app
    r=next(x for x in app.routes
           if x.path=="/api/live/evm/permission-status/{asset}")
    source=inspect.getsource(r.endpoint)
    assert "verify_privy_bearer" in source
    assert r.methods=={"GET"}
    assert "read_owner_evm_permission_status" in source


def test_status_reader_rejects_unsupported_coin():
    with pytest.raises(HTTPException) as err:
        read_owner_evm_permission_status(DID,asset="ETH")
    assert err.value.status_code==404



def test_ethereum_token_usdt_settlement_status_uses_saved_user_choice():
    # UNI and LINK allow either supported Ethereum stablecoin. A USDT
    # trading grant must not disappear just because the status view used
    # to hard-code USDC.
    for asset in ("UNI","LINK"):
        row=stored(asset)
        row["stable"]="USDT"
        result=assess_evm_permission_record(row,asset=asset,now=NOW)
        assert result["status"]=="GRANT_VERIFIED"
        assert result["signer_grant_recorded"] is True
