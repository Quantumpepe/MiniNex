"""Verify existing original Privy EVM wallet and one aggregate policy exactly."""
from copy import deepcopy

import pytest

from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,
    verified_wallet_policy_digest,
)
from backend.shared_evm_wallet_policy_remote_review import (
    review_original_one_signer_wallet, verify_remote_combined_policy,
)

DID="did:privy:owner12345"
WALLET="0x"+"a"*40
WALLET_ID="wallet_12345678"
OWNER="owner_12345678"
SIGNER="signer_12345678"
POLICY="policy_12345678"
NOW=1800000000
ASSETS=("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB")
NATIVE=frozenset(("ETH","BNB","POL","AVAX"))


def scopes_all():
    return [dict(
        asset=asset,
        stable=("USDT" if asset=="BNB" else "USDC"),
        treasury="0x"+"b"*40,
        buy_source_atomic_cap="5000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=(
            "1000000000000000000" if asset in NATIVE else None
        ),
        owner_max_fee_atomic="1000000",
        expires_at=NOW+3600,
    ) for asset in ASSETS]


def original(scopes):
    args=dict(
        wallet=WALLET,owner_id=OWNER,executor_id=SIGNER,
        scopes=scopes,now=NOW,
    )
    expected=compose_scoped_evm_wallet_policy(**args)
    digest=verified_wallet_policy_digest(expected,**args)
    remote=deepcopy(expected)
    remote["id"]=POLICY
    remote["rules"]=[
        dict(x,id="rule_"+str(i+1).zfill(8))
        for i,x in enumerate(remote["rules"])
    ]
    user={"id":DID,"linked_accounts":[{
        "type":"wallet","chain_type":"ethereum",
        "wallet_client_type":"privy","id":WALLET_ID,
        "address":WALLET,"delegated":True,
    }]}
    metadata={
        "id":WALLET_ID,"address":WALLET,
        "chain_type":"ethereum",
        "entity":{"type":"user","id":DID},
        "archived_at":None,
        "additional_signers":[{
            "signer_id":SIGNER,
            "override_policy_ids":[POLICY],
        }],
    }
    grant=dict(
        wallet_id=WALLET_ID,wallet_address=WALLET,
        policy_id=POLICY,signer_id=SIGNER,owner_id=OWNER,
        policy_sha256=digest,status="GRANT_VERIFIED",
        legacy_reconciliation_required=False,
    )
    return dict(
        did=DID,user=user,wallet_metadata=metadata,
        remote_policy=remote,saved_grant=grant,
        scopes=scopes,now=NOW,
    )


def test_all_eight_evm_coins_share_one_exact_remote_privy_policy():
    data=original(scopes_all())
    expected=compose_scoped_evm_wallet_policy(
        wallet=WALLET,owner_id=OWNER,executor_id=SIGNER,
        scopes=data["scopes"],now=NOW,
    )
    assert len(expected["rules"]) > 7
    assert verify_remote_combined_policy(data["remote_policy"],expected=expected)==POLICY
    r=review_original_one_signer_wallet(**data)
    assert r["one_scoped_signer_verified"] is True
    assert r["remote_policy_exactly_verified"] is True
    assert r["scope_coins"]==sorted(ASSETS)
    assert r["transaction_sent"] is False
    assert r["authorization_changed_by_this_review"] is False


@pytest.mark.parametrize("mutate",[
    lambda d:d["remote_policy"]["rules"][0].update(method="*"),
    lambda d:d["remote_policy"]["rules"][-1]["conditions"].pop(),
    lambda d:d["remote_policy"]["rules"][2]["conditions"][1].update(
        value="0x"+"f"*40
    ),
    lambda d:d["remote_policy"]["rules"].append(
        deepcopy(d["remote_policy"]["rules"][0])
    ),
    lambda d:d["remote_policy"]["rules"][1].update(
        id=d["remote_policy"]["rules"][0]["id"]
    ),
    lambda d:d["remote_policy"]["rules"][0].update(action="DENY"),
    lambda d:d["remote_policy"].update(owner_id="other_owner12345"),
    lambda d:d["remote_policy"].update(id="another_policy_12345"),
    lambda d:d["wallet_metadata"]["additional_signers"][0].update(
        override_policy_ids=[]
    ),
    lambda d:d["wallet_metadata"]["additional_signers"].append({
        "signer_id":"unexpected_signer_12345","override_policy_ids":[],
    }),
    lambda d:d["saved_grant"].update(legacy_reconciliation_required=True),
    lambda d:d["saved_grant"].update(policy_sha256="a"*64),
    lambda d:d["user"]["linked_accounts"][0].update(delegated=False),
    lambda d:d["wallet_metadata"]["entity"].update(id="did:privy:other12345"),
])
def test_tampered_remote_scope_and_unreviewed_signer_are_not_accepted(mutate):
    values=original(scopes_all())
    mutate(values)
    with pytest.raises((ValueError,KeyError)):
        review_original_one_signer_wallet(**values)


def test_empty_policy_and_unverified_owner_are_never_signer_authority():
    values=original(scopes_all()[:1])
    values["saved_grant"]["status"]="DRAFT"
    with pytest.raises(ValueError):
        review_original_one_signer_wallet(**values)
