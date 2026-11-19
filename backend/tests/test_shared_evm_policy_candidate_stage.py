"""Policy candidates can only be staged after EIP191 signed coin consent."""
import inspect
from datetime import datetime,timezone

import pytest
from fastapi import HTTPException

from backend.shared_evm_policy_candidate_stage import _stage_locked

DID="did:privy:owner"
WALLET="0x"+"ab"*20
OWNED={"wallet_id":"wallet_1234567","address":WALLET}
SHA="a"*64
NOW=1800000000
CHAINS={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1}


class Cur:
    def __init__(self,rows):self.rows=iter(rows);self.sql=[]
    def execute(self,sql,args):self.sql.append((sql,args))
    def fetchone(self):return next(self.rows)


def rows(asset):
    stable="USDT" if asset=="BNB" else "USDC"
    return [
        {"stable":stable,"budget_usd":100000,"revision":5},
        {"status":"SIGNED_INTENT","wallet_id":OWNED["wallet_id"],
         "wallet_address":WALLET,"chain_id":CHAINS[asset],
         "stable":stable,"budget_revision":5,"budget_usd":100000,
         "message_sha256":SHA,
         "confirmed_at":datetime.fromtimestamp(NOW,timezone.utc)},
        None,{"status":"DRAFT"},
    ]


def stage(cur,asset):
    return _stage_locked(
        cur,did=DID,asset=asset,owned=OWNED,
        owner_id="owner_1234567",signer_id="signer_1234567",
        buy_source_atomic_cap="100000000000",
        sell_source_atomic_cap="10000000000000000000",
        native_wrap_atomic_cap=(
            "10000000000000000000" if asset in ("BNB","POL","AVAX") else None),
        expires_at=NOW+900,now=NOW)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_five_assets_stage_single_immutable_signed_owner_policy(asset):
    cur=Cur(rows(asset))
    report=stage(cur,asset)
    assert report["asset"]==asset
    assert report["chain_id"]==CHAINS[asset]
    assert report["rules_count"]==(4 if asset in ("BNB","POL","AVAX") else 2)
    assert len(report["policy_sha256"])==64
    assert report["signed_budget_revision"]==5
    assert report["policy_published_in_privy"] is False
    assert report["owner_delegation_granted"] is False
    assert len(cur.sql)==4
    assert "mininex_live_evm_owner_permissions" in cur.sql[3][0]
    assert "ON CONFLICT" in cur.sql[3][0]


@pytest.mark.parametrize("change",[
    lambda r:r[1].update(status="PENDING"),
    lambda r:r[1].update(wallet_address="0x"+"cc"*20),
    lambda r:r[1].update(wallet_id="wallet_other123"),
    lambda r:r[1].update(budget_revision=4),
    lambda r:r[1].update(message_sha256="invalid"),
    lambda r:r[1].update(chain_id=1),
    lambda r:r[1].update(confirmed_at=None),
])
def test_policy_can_never_be_staged_from_unsigned_or_other_wallet_intent(change):
    data=rows("AVAX")
    change(data)
    cur=Cur(data)
    with pytest.raises(HTTPException) as e:
        stage(cur,"AVAX")
    assert e.value.status_code==409
    assert len(cur.sql)==2


def test_preexisting_or_revoked_owner_policy_cannot_be_replaced():
    data=rows("LINK")
    data[2]={"status":"REVOKED"}
    cur=Cur(data)
    with pytest.raises(HTTPException):
        stage(cur,"LINK")
    assert len(cur.sql)==3


def test_candidate_staging_is_internal_only_and_never_posts_privy_or_wallet_rpc():
    from backend import shared_evm_policy_candidate_stage as m
    source=inspect.getsource(m)
    assert "post_claimed_privy_evm_rpc" not in source
    assert "client.post" not in source
    assert "sign_privy" not in source
    from backend.app import app
    assert all("evm-policy-stage" not in r.path for r in app.routes)
