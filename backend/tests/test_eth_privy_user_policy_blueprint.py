"""Privy per-user ETH policy drafts are NEVER enrollment or signing."""
from copy import deepcopy

import pytest

from backend import app as api
from backend import eth_privy_user_policy_blueprint as template
from backend.eth_swaprouter02_preflight import SWAPROUTER02_MAINNET, WETH_MAINNET
from backend.live_eth_quotes import STABLES

WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
OWNER = "policy_owner_admin_quorum"
SIGNER = "mininex_backend_executor_quorum"
NOW = 1_900_000_000
EXPIRES = NOW + 7 * 86400

CAPS = {
    "usdc_max_input_micro": "5000000000",
    "usdt_max_input_micro": "5000000000",
    "weth_max_input_wei": "1500000000000000000",
    "native_wrap_max_wei": "1500000000000000000",
}


def args(**overrides):
    return {
        "wallet": WALLET,
        "policy_owner_id": OWNER,
        "executor_signer_id": SIGNER,
        "expires_at": EXPIRES,
        "now": NOW,
        **CAPS,
        **overrides,
    }


def build(**overrides):
    return template.build_eth_privy_user_policy_blueprint(**args(**overrides))


def verify(policy, **overrides):
    return template.inspect_eth_user_policy_blueprint(policy, **args(**overrides))


def field(rule, source, name, operator=None):
    found = [
        entry for entry in rule["conditions"]
        if entry["field_source"] == source and entry["field"] == name
        and (operator is None or entry["operator"] == operator)
    ]
    assert len(found) == 1
    return found[0]


def test_two_wallets_get_distinct_policy_recipients_and_owner_control():
    first = build()
    second = build(wallet=OTHER)
    assert first != second
    for p, expected_wallet in ((first, WALLET), (second, OTHER)):
        assert p["name"] == "MiniNex-ETH-" + expected_wallet[-8:]
        assert p["version"] == "1.0"
        assert p["chain_type"] == "ethereum"
        assert p["owner_id"] == OWNER
        assert len(p["rules"]) == 6
        assert all(r["method"] == "eth_sendTransaction" for r in p["rules"])
        assert all(r["action"] == "ALLOW" for r in p["rules"])
        for rule in p["rules"][:4]:
            assert field(
                rule, "ethereum_calldata", "exactInputSingle.params.recipient",
                "eq",
            )["value"] == expected_wallet
        assert verify(p, wallet=expected_wallet)["policy_draft_matches_expected"]


def test_each_allow_rule_is_individually_restricted_not_accidentally_or_broad():
    p = build()
    for rule in p["rules"]:
        chain = field(rule, "ethereum_transaction", "chain_id", "eq")
        assert chain["value"] == "1"
        expiration = field(
            rule, "system", "current_unix_timestamp", "lt"
        )
        assert int(expiration["value"]) == EXPIRES
        assert field(rule, "ethereum_transaction", "to", "eq")["value"] in (
            SWAPROUTER02_MAINNET, WETH_MAINNET,
        )
        assert field(rule, "ethereum_calldata", "function_name", "eq")[
            "value"
        ] in ("exactInputSingle", "deposit", "withdraw")
        assert all(
            condition.get("operator") in (
                "eq", "gt", "lte", "in", "lt"
            ) for condition in rule["conditions"]
        )
    assert all(r["name"].startswith("MiniNex ETH") for r in p["rules"])


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("stable", ["USDC", "USDT"])
def test_swap_rules_fix_token_pair_recipient_nonzero_min_and_per_wallet_cap(
    stable, side,
):
    p = build()
    rule = next(r for r in p["rules"] if r["name"] ==
                f"MiniNex ETH {side} {stable} exactInputSingle")
    assert field(rule, "ethereum_transaction", "to", "eq")[
        "value"
    ] == SWAPROUTER02_MAINNET
    assert field(rule, "ethereum_transaction", "value", "eq")["value"] == "0"
    expected_in = STABLES[stable] if side == "BUY" else WETH_MAINNET
    expected_out = WETH_MAINNET if side == "BUY" else STABLES[stable]
    assert field(rule, "ethereum_calldata", "exactInputSingle.params.tokenIn")[
        "value"
    ] == expected_in
    assert field(rule, "ethereum_calldata", "exactInputSingle.params.tokenOut")[
        "value"
    ] == expected_out
    assert field(rule, "ethereum_calldata", "exactInputSingle.params.recipient")[
        "value"
    ] == WALLET
    assert field(rule, "ethereum_calldata", "exactInputSingle.params.fee")[
        "value"
    ] == ["500", "3000"]
    assert field(
        rule, "ethereum_calldata", "exactInputSingle.params.sqrtPriceLimitX96",
    )["value"] == "0"
    assert field(
        rule, "ethereum_calldata", "exactInputSingle.params.amountIn", "gt"
    )["value"] == "0"
    assert field(
        rule, "ethereum_calldata", "exactInputSingle.params.amountIn", "lte"
    )["value"] == CAPS[f"{stable.lower()}_max_input_micro"] if side == "BUY" else CAPS["weth_max_input_wei"]
    assert field(
        rule, "ethereum_calldata", "exactInputSingle.params.amountOutMinimum",
        "gt",
    )["value"] == "0"
    assert all(
        c.get("abi") == template.EXACT_INPUT_SINGLE_ABI
        for c in rule["conditions"]
        if c["field_source"] == "ethereum_calldata"
    )


def test_weth_rules_have_separate_native_value_and_wad_limits():
    policy = build()
    deposit = policy["rules"][4]
    withdraw = policy["rules"][5]
    assert field(deposit, "ethereum_transaction", "to")["value"] == WETH_MAINNET
    assert field(deposit, "ethereum_calldata", "function_name")["value"] == "deposit"
    assert field(deposit, "ethereum_transaction", "value", "gt")["value"] == "0"
    assert field(deposit, "ethereum_transaction", "value", "lte")[
        "value"
    ] == CAPS["native_wrap_max_wei"]
    assert field(withdraw, "ethereum_calldata", "function_name")[
        "value"
    ] == "withdraw"
    assert field(withdraw, "ethereum_transaction", "value", "eq")[
        "value"
    ] == "0"
    assert field(withdraw, "ethereum_calldata", "withdraw.wad", "gt")[
        "value"
    ] == "0"
    assert field(withdraw, "ethereum_calldata", "withdraw.wad", "lte")[
        "value"
    ] == CAPS["weth_max_input_wei"]
    assert field(withdraw, "ethereum_calldata", "withdraw.wad")[
        "abi"
    ] == template.WETH_WITHDRAW_ABI


@pytest.mark.parametrize("patch", [
    {"policy_owner_id": SIGNER},
    {"policy_owner_id": ""},
    {"policy_owner_id": "bad id"},
    {"executor_signer_id": OWNER},
    {"wallet": ""},
    {"wallet": "0x123"},
    {"wallet": None},
    {"usdc_max_input_micro": "0"},
    {"usdc_max_input_micro": "100.00"},
    {"usdc_max_input_micro": "01"},
    {"usdc_max_input_micro": "0x1"},
    {"usdc_max_input_micro": "1e6"},
    {"usdt_max_input_micro": None},
    {"weth_max_input_wei": "-1"},
    {"native_wrap_max_wei": True},
    {"native_wrap_max_wei": "999999999999999999999999999999999999999999999"},
    {"expires_at": NOW - 1},
    {"expires_at": NOW + 30 * 86400 + 1},
    {"expires_at": NOW},
    {"now": True},
])
def test_invalid_owner_wallet_caps_or_lifetime_fails_closed(patch):
    with pytest.raises(ValueError):
        build(**patch)


@pytest.mark.parametrize("mutation", [
    lambda p: p["rules"][0]["conditions"].pop(0),
    lambda p: p["rules"][0]["conditions"].pop(1),
    lambda p: p["rules"][0]["conditions"].pop(2),
    lambda p: p["rules"][0]["conditions"].pop(3),
    lambda p: p["rules"][0]["conditions"].pop(4),
    lambda p: p["rules"][0]["conditions"].pop(5),
    lambda p: p["rules"][0]["conditions"].pop(6),
    lambda p: p["rules"][0]["conditions"].pop(7),
    lambda p: p["rules"][0]["conditions"].pop(8),
    lambda p: p["rules"][0]["conditions"].pop(9),
    lambda p: p["rules"][0]["conditions"].pop(10),
    lambda p: p["rules"][0]["conditions"].pop(11),
    lambda p: p["rules"][0]["conditions"].pop(12),
    lambda p: p["rules"][0]["conditions"][-1].update(value="1"),
    lambda p: p["rules"][0]["conditions"][1].update(value="0x" + "f" * 40),
    lambda p: p["rules"][0].update(method="*"),
    lambda p: p["rules"][0].update(action="DENY"),
    lambda p: p["rules"].append({
        "name": "open sender", "method": "eth_sendTransaction",
        "action": "ALLOW", "conditions": [],
    }),
    lambda p: p["rules"].append({
        "name": "permit", "method": "eth_signTypedData_v4",
        "action": "ALLOW", "conditions": [],
    }),
    lambda p: p["rules"].append({
        "name": "export", "method": "exportPrivateKey",
        "action": "ALLOW", "conditions": [],
    }),
    lambda p: p.update(owner_id=SIGNER),
    lambda p: p.update(chain_type="solana"),
])
def test_no_looser_rule_or_missing_condition_can_pass_static_draft_review(
    mutation,
):
    policy = deepcopy(build())
    mutation(policy)
    reviewed = verify(policy)
    assert reviewed["policy_draft_matches_expected"] is False
    assert reviewed["wallet_bound"] is False
    assert reviewed["safe_to_sign"] is False


def test_blueprint_caps_are_per_customer_not_fixed_100_usdc():
    first = build()
    bigger = build(
        usdc_max_input_micro="50000000000",
        usdt_max_input_micro="50000000000",
        weth_max_input_wei="5000000000000000000",
        native_wrap_max_wei="5000000000000000000",
    )
    assert first != bigger
    assert verify(first)["policy_draft_matches_expected"] is True
    assert verify(first, usdc_max_input_micro="50000000000")[
        "policy_draft_matches_expected"
    ] is False
    assert verify(bigger, usdc_max_input_micro="50000000000",
                  usdt_max_input_micro="50000000000",
                  weth_max_input_wei="5000000000000000000",
                  native_wrap_max_wei="5000000000000000000")[
        "policy_draft_matches_expected"
    ] is True


def test_blueprint_does_not_create_permissions_or_API_writes():
    p = build()
    report = verify(p)
    assert report["allow_rules"] == 6
    assert report["wallet_bound"] is True
    assert report["policy_owner_separate_from_executor"] is True
    assert report["privy_policy_created"] is False
    assert report["user_consent_verified"] is False
    assert report["live_limits_verified_from_server"] is False
    assert report["wallet_signer_registered"] is False
    assert report["erc20_allowance_plan_implemented"] is False
    assert report["untrusted_price_slippage_protected_by_policy"] is False
    assert report["privy_abi_semantics_tested"] is False
    assert report["execution_enabled"] is False
    assert report["safe_to_sign"] is False
    assert report["can_submit"] is False
    paths = {r.path for r in api.app.routes}
    assert "/api/live/eth/privy/create-policy" not in paths
    assert "/api/live/eth/privy/grant" not in paths
    assert "/api/live/eth/policy-draft" not in paths
