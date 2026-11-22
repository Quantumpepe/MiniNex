"""One central signer: owner-consented wallet policy changes are not grants."""
from copy import deepcopy

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,
)
from backend.shared_evm_wallet_scope_transition import (
    plan_wallet_policy_transition, verify_wallet_policy_update_intent,
)

_KEY = bytes.fromhex("01" * 32)
_WALLET = Account.from_key(_KEY).address
_DID = "did:privy:owner12345"
_WALLET_ID = "wallet_12345678"
_OWNER = "owner_123456"
_SIGNER = "backend_123456"
_NOW = 1800000000
_NONCE = "a" * 64


def scoped(asset, stable="USDC", native=False, cap="5000000"):
    return dict(
        asset=asset,
        stable=stable,
        treasury="0x" + "ab" * 20,
        buy_source_atomic_cap=cap,
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=(
            "1000000000000000000" if native else None
        ),
        owner_max_fee_atomic="1000000",
        expires_at=_NOW + 3600,
    )


def policy(scopes):
    return compose_scoped_evm_wallet_policy(
        wallet=_WALLET, owner_id=_OWNER,
        executor_id=_SIGNER, scopes=scopes, now=_NOW,
    )


def plan(old, new, **kwargs):
    data = dict(
        did=_DID, wallet=_WALLET, wallet_id=_WALLET_ID,
        owner_id=_OWNER, executor_id=_SIGNER,
        existing_scopes=old, desired_scopes=new,
        existing_policy=policy(old) if old else None,
        now=_NOW, nonce=_NONCE,
    )
    data.update(kwargs)
    return plan_wallet_policy_transition(**data)


def signed(plan_data):
    return "0x" + Account.sign_message(
        encode_defunct(text=plan_data["intent_challenge"]),
        private_key=_KEY,
    ).signature.hex()


def check(plan_data, sig, **kwargs):
    data = dict(
        plan=plan_data, expected_did=_DID,
        expected_wallet=_WALLET, expected_wallet_id=_WALLET_ID,
        expected_previous_digest=plan_data["previous_policy_sha256"],
        expected_target_digest=plan_data["target_policy_sha256"],
        signature=sig, now=_NOW,
    )
    data.update(kwargs)
    return verify_wallet_policy_update_intent(**data)


def test_initial_owner_add_signer_is_the_only_required_user_confirmation():
    result = plan([], [scoped("ETH", native=True)])
    assert result["state"] == "INITIAL_OWNER_ADD_SIGNER_REQUIRED"
    assert result["owner_wallet_confirmation_required"] is True
    assert result["intent_eip191_required"] is False
    assert result["intent_challenge"] is None
    assert result["policy_created_in_privy"] is False


def test_existing_evm_wallet_new_coin_requires_one_sign_message_not_second_signer():
    original = [scoped("ETH", native=True)]
    target = [*original, scoped("QNT"), scoped("LINK")]
    result = plan(original, target)
    assert result["state"] == "OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED"
    assert result["coins_changed"] == ["LINK", "QNT"]
    assert result["shared_signer_id"] == _SIGNER
    assert "Previous policy SHA256:" in result["intent_challenge"]
    assert "Requested policy SHA256:" in result["intent_challenge"]
    assert "Wallet ID: " + _WALLET_ID in result["intent_challenge"]
    assert check(result, signed(result)) is True
    assert check(result, "0x" + "00" * 65) is False
    assert result["signer_granted_or_changed"] is False
    assert result["transaction_sent"] is False


def test_no_new_confirmation_when_scopes_exactly_unchanged():
    old = [scoped("LINK"), scoped("BNB", stable="USDT", native=True)]
    result = plan(old, list(reversed(old)))
    assert result["state"] == "UNCHANGED"
    assert result["owner_wallet_confirmation_required"] is False
    assert result["intent_eip191_required"] is False


def test_remove_only_narrows_policy_without_granting_new_coin():
    old = [scoped("LINK"), scoped("UNI")]
    result = plan(old, [old[0]])
    assert result["state"] == "REMOVE_UNNEEDED_COIN_RULES_ONLY"
    assert result["coins_changed"] == []
    assert result["coins_removed"] == ["UNI"]
    assert result["owner_wallet_confirmation_required"] is False


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(buy_source_atomic_cap="7000000"),
    lambda data: data.update(owner_max_fee_atomic="2000000"),
    lambda data: data.update(treasury="0x" + "bc" * 20),
    lambda data: data.update(expires_at=_NOW + 7300),
    lambda data: data.update(stable="USDT"),
])
def test_any_scope_expansion_or_recipient_change_needs_wallet_signature(mutation):
    old = [scoped("LINK")]
    changed = deepcopy(old[0])
    mutation(changed)
    p = plan(old, [changed])
    assert p["state"] == "OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED"
    assert check(p, signed(p))


@pytest.mark.parametrize("change", [
    {"expected_did": "did:privy:other12345"},
    {"expected_wallet_id": "wallet_other12345"},
    {"expected_wallet": "0x" + "a" * 40},
    {"expected_previous_digest": "0" * 64},
    {"expected_target_digest": "f" * 64},
    {"now": _NOW + 300},
    {"now": _NOW - 1},
])
def test_wrong_owner_wallet_digest_or_expiry_is_not_approval(change):
    p = plan([scoped("LINK")], [scoped("UNI")])
    assert check(p, signed(p), **change) is False


def test_wrong_signer_and_remote_policy_tampering_is_rejected_before_intent():
    old = [scoped("LINK")]
    original = policy(old)
    original["rules"][0]["method"] = "*"
    with pytest.raises(ValueError):
        plan(old, [*old, scoped("QNT")], existing_policy=original)
    with pytest.raises(ValueError):
        plan(old, [scoped("QNT")], executor_id=_OWNER)
    with pytest.raises(ValueError):
        plan(old, [scoped("QNT")], nonce="not-a-nonce")


def test_empty_target_is_revoke_not_an_empty_allow_policy():
    with pytest.raises(ValueError):
        plan([scoped("LINK")], [])


def test_only_original_wallet_eip191_signature_is_accepted():
    p = plan([scoped("LINK")], [scoped("UNI")])
    wrong_sig = "0x" + Account.sign_message(
        encode_defunct(text=p["intent_challenge"]),
        private_key=bytes.fromhex("02" * 32),
    ).signature.hex()
    assert check(p, wrong_sig) is False
