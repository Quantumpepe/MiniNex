"""No silent fee permission: separate ERC20 Treasury-only Privy draft."""
from copy import deepcopy

import pytest

from backend.shared_evm_fee_privy_policy_draft import (
    build_owner_evm_fee_policy_draft,
    inspect_owner_evm_fee_policy_draft,
)
from backend.shared_evm_swap_receipts import _pair

WALLET = "0x" + "aa"*20
TREASURY = "0x" + "bb"*20
OWNER = "owner_12345678"
EXECUTOR = "signer_12345678"

ASSETS = [
    ("BNB", "USDT"), ("POL", "USDC"), ("AVAX", "USDC"),
    ("LINK", "USDC"), ("UNI", "USDT"), ("ARB", "USDC"), ("ARB", "USDT"),
]


def make(asset, stable):
    return dict(
        asset=asset, stable=stable, wallet=WALLET, treasury=TREASURY,
        owner_id=OWNER, executor_id=EXECUTOR,
        owner_max_fee_atomic="1234567",
        now=1800000, expires_at=1800000 + 86400*30,
    )


@pytest.mark.parametrize("asset,stable", ASSETS)
def test_one_owner_approved_fee_erc20_transfer_only(asset, stable):
    args = make(asset, stable)
    policy = build_owner_evm_fee_policy_draft(**args)
    assert len(policy["rules"]) == 1
    rule = policy["rules"][0]
    assert rule["method"] == "eth_sendTransaction"
    assert rule["action"] == "ALLOW"
    conditions = rule["conditions"]
    chain = _pair(asset, stable, "BUY")
    assert any(c.get("field") == "chain_id" and c.get("value") ==
               str(chain["chain_id"]) for c in conditions)
    assert any(c.get("field") == "to" and c.get("value") ==
               chain["source_token"] for c in conditions)
    assert any(c.get("field") == "transfer.to" and c.get("value") ==
               TREASURY for c in conditions)
    assert any(c.get("field") == "transfer.value" and c.get("operator") ==
               "lte" and c.get("value") == "1234567" for c in conditions)
    assert not any(c.get("field") == "approve.spender" for c in conditions)
    review = inspect_owner_evm_fee_policy_draft(policy, **args)
    assert review["strict_treasury_transfer_draft_matches"] is True
    assert review["automatic_fee_transfer_enabled"] is False
    assert review["user_fee_consent_verified"] is False
    assert review["can_submit"] is False


@pytest.mark.parametrize("mutation", [
    lambda p: p["rules"][0]["conditions"].pop(),
    lambda p: p["rules"][0]["conditions"][1].update(value=WALLET),
    lambda p: p["rules"][0]["conditions"][5].update(value=WALLET),
    lambda p: p["rules"][0]["conditions"][7].update(value="999999999"),
    lambda p: p["rules"][0].update(action="ALLOW_ALL"),
    lambda p: p["rules"].append({"action":"ALLOW","conditions":[]}),
])
def test_scope_mutation_never_matches_exact_user_fee_policy(mutation):
    args = make("LINK", "USDC")
    altered = deepcopy(build_owner_evm_fee_policy_draft(**args))
    mutation(altered)
    result = inspect_owner_evm_fee_policy_draft(altered, **args)
    assert result["strict_treasury_transfer_draft_matches"] is False
    assert result["policy_sha256"] is None


@pytest.mark.parametrize("change", [
    lambda x: x.update(treasury=WALLET),
    lambda x: x.update(owner_id=EXECUTOR),
    lambda x: x.update(owner_max_fee_atomic="0"),
    lambda x: x.update(owner_max_fee_atomic="1000000000000000000000000000000000000000000000"),
    lambda x: x.update(expires_at=1800000),
    lambda x: x.update(stable="BTC"),
])
def test_invalid_fee_owner_grant_draft_refused(change):
    args = make("UNI","USDT")
    change(args)
    with pytest.raises((ValueError, KeyError)):
        build_owner_evm_fee_policy_draft(**args)


def test_fee_policy_never_changes_trading_policy_or_publishes_wallet_grant():
    import inspect
    import backend.shared_evm_fee_privy_policy_draft as module
    src = inspect.getsource(module)
    assert "post_claimed_privy" not in src
    assert "publish_user_evm_privy_policy" not in src
    assert "sign_privy_rpc_payload" not in src
    assert "psycopg" not in src
