"""OFFLINE draft for a separate owner-approved automatic 5% fee privilege.

This is deliberately NOT a Privy grant, transaction signer, wallet delegation,
or fee collector. It can only be considered after real verified net-PnL
assessments, per-owner treasury consent and a remote policy audit exist.

Unlike the trading policy, only exact ERC20 transfer(wallet -> configured
treasury) may be authorized. No router/approve/wildcard or other recipient.
The wallet owner selects an explicit per-transfer atomic maximum; no hidden
platform order, daily turnover or dollar amount limit is introduced.
"""
from __future__ import annotations

import hashlib
import json
import re

from .eth_privy_user_policy_blueprint import _tx, _call, _expiry, _positive_atomic
from .shared_evm_swap_receipts import _pair

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_ASSETS = frozenset(("BNB", "POL", "AVAX", "LINK", "UNI"))

ERC20_TRANSFER_ABI = [{
    "type": "function", "name": "transfer",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
    ],
    "outputs": [{"name": "", "type": "bool"}],
}]


def build_owner_evm_fee_policy_draft(
    *,asset: str, stable: str, wallet: str, treasury: str,
    owner_id: str, executor_id: str, owner_max_fee_atomic: str,
    expires_at: int, now: int,
) -> dict:
    """Only one ERC20 transfer rule; amount cap belongs to user's consent."""
    if asset not in _ASSETS:
        raise ValueError("Unsupported scoped EVM fee policy asset")
    pair = _pair(asset, stable, "BUY")
    if (type(wallet) is not str or not _ADDR.fullmatch(wallet)
            or type(treasury) is not str or not _ADDR.fullmatch(treasury)
            or wallet.lower() == treasury.lower()
            or type(owner_id) is not str or not _ID.fullmatch(owner_id)
            or type(executor_id) is not str or not _ID.fullmatch(executor_id)
            or owner_id == executor_id
            or type(now) is not int or now <= 0
            or type(expires_at) is not int or expires_at <= now):
        raise ValueError("Explicit owner/treasury/expiry consent data required")
    cap = _positive_atomic(owner_max_fee_atomic)
    token = pair["source_token"]
    return {
        "version": "1.0",
        "chain_type": "ethereum",
        "name": f"MiniNex-fee-{asset}-{wallet[-8:].lower()}",
        "owner_id": owner_id,
        "rules": [{
            "name": f"MiniNex {asset} {stable} owner-approved net-profit fee",
            "method": "eth_sendTransaction",
            "action": "ALLOW",
            "conditions": [
                _tx("chain_id", "eq", str(pair["chain_id"])),
                _tx("to", "eq", token),
                _expiry(expires_at),
                _tx("value", "eq", "0"),
                _call("function_name", "eq", "transfer", ERC20_TRANSFER_ABI),
                _call("transfer.to", "eq", treasury, ERC20_TRANSFER_ABI),
                _call("transfer.value", "gt", "0", ERC20_TRANSFER_ABI),
                _call("transfer.value", "lte", cap, ERC20_TRANSFER_ABI),
            ],
        }],
    }


def inspect_owner_evm_fee_policy_draft(
    candidate: object, **kwargs,
) -> dict:
    """Static, full-object comparison; never evidence of wallet approval."""
    expected = build_owner_evm_fee_policy_draft(**kwargs)
    matched = type(candidate) is dict and candidate == expected
    digest = hashlib.sha256(json.dumps(
        expected, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "mode": "OWNER_EVM_FEE_PRIVY_POLICY_OFFLINE_DRAFT_ONLY",
        "asset": kwargs["asset"],
        "stable": kwargs["stable"],
        "chain_id": _pair(kwargs["asset"], kwargs["stable"], "BUY")["chain_id"],
        "strict_treasury_transfer_draft_matches": matched,
        "policy_sha256": digest if matched else None,
        "privy_policy_published": False,
        "user_fee_consent_verified": False,
        "additional_signer_granted": False,
        "net_profit_source_verified": False,
        "automatic_fee_transfer_enabled": False,
        "can_submit": False,
    }
