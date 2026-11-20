"""Original BTC/SOL/XRP Ethereum BUY requires current real chain RPC evidence."""
import asyncio
import copy
import hashlib
import json

import pytest
from fastapi import HTTPException

from backend import native_eth_native_buy_preflight as m
from backend.native_eth_thor_deposit_tx import encode_original_eth_native_buy_deposit
from backend.tests.test_native_eth_thor_deposit_tx import (
    NOW, ETH, VAULT, ROUTER, quote,
)

_AMOUNT = "50"
_STABLE = "USDC"
_FUNDS = 100_000_000
_ETH_WEI = 10**17
_GAS = 120_000
_GAS_PRICE = 25_000_000_000


def preview(asset="BTC", stable=_STABLE, amount=_AMOUNT):
    q = quote(asset=asset, stable=stable, amount=amount)
    return encode_original_eth_native_buy_deposit(
        q, asset=asset, stable=stable, amount=amount, now=NOW)


def inspect(**override):
    args = {
        "unsigned": preview(), "original_quote": quote(), "now": NOW,
        "chain_id": "0x1",
        "token_allowance": "0x" + f"{_FUNDS:064x}",
        "eth_balance": hex(_ETH_WEI),
        "token_balance": "0x" + f"{_FUNDS:064x}",
        "pending_nonce": "0x2a",
        "estimate_gas": hex(_GAS),
        "gas_price": hex(_GAS_PRICE),
    }
    args.update(override)
    return m.inspect_original_native_buy_evm_preflight(**args)


@pytest.mark.parametrize("asset,stable,amount", [
    ("BTC", "USDC", "50"),
    ("SOL", "USDT", "0.01"),
    ("XRP", "USDC", "50"),
])
def test_native_buy_wallet_router_token_vault_and_current_gas_verified(asset,stable,amount):
    tx = preview(asset=asset,stable=stable,amount=amount)
    r = inspect(unsigned=tx, original_quote=quote(asset,stable,amount))
    assert r["mode"] == "THORCHAIN_ETH_ORIGINAL_NATIVE_BUY_NETWORK_PREFLIGHT"
    assert r["asset"] == asset and r["stable"] == stable
    assert r["source_wallet"] == ETH
    assert r["router"] == ROUTER
    assert r["vault"] == VAULT
    assert r["chain_id_verified"] is True
    assert r["original_router_allowance_verified"] is True
    assert r["source_funds_and_gas_verified"] is True
    assert r["pending_nonce_and_gas_estimate_verified"] is True
    assert r["unsigned_transaction"]["nonce"] == "0x2a"
    assert r["unsigned_transaction"]["gas"] == hex(_GAS)
    assert r["unsigned_transaction"]["gasPrice"] == hex(_GAS_PRICE)
    assert len(r["unsigned_transaction_sha256"]) == 64
    for flag in (
        "independently_verified_privy_owner_grant", "durable_native_order_claim",
        "ready_to_sign", "wallet_signed", "source_transaction_sent",
        "execution_enabled", "can_submit",
    ):
        assert r[flag] is False


@pytest.mark.parametrize("override", [
    {"chain_id":"0x2"},
    {"chain_id":"0x01"},
    {"token_allowance":"0x" + f"{49_000_000:064x}"},
    {"token_allowance":"0x0"},
    {"token_balance":"0x" + f"{49_000_000:064x}"},
    {"token_balance":"0x0"},
    {"eth_balance":"0x0"},
    {"eth_balance":"NaN"},
    {"pending_nonce":"bad"},
    {"estimate_gas":"0x0"},
    {"estimate_gas":"0x1"},
    {"gas_price":"0x0"},
])
def test_native_buy_does_not_guess_balance_allowance_gas_or_nonce(override):
    with pytest.raises(ValueError):
        inspect(**override)


def test_native_buy_preflight_rechecks_expiry_after_network_reads():
    with pytest.raises(ValueError):
        inspect(now=NOW+121)
    with pytest.raises(ValueError):
        inspect(now=NOW+90*60+1)


@pytest.mark.parametrize("field,value", [
    ("vault", "0x" + "f"*40),
    ("router", "0x" + "f"*40),
    ("source_wallet", "0x" + "f"*40),
    ("source_amount_atomic", "49000000"),
    ("destination_wallet", "not-native"),
    ("ready_to_sign", True),
    ("can_submit", True),
    ("wallet_signed", True),
])
def test_native_buy_unsigned_draft_cannot_be_mutated_into_send_permission(field,value):
    unsigned = preview()
    unsigned[field] = value
    with pytest.raises(ValueError):
        inspect(unsigned=unsigned)


def test_native_buy_alternate_token_contract_or_refund_rejected():
    original = preview()
    calldata = original["unsigned_transaction"]["data"]
    # Tamper with ABI-padded official USDC address while preserving layout.
    mutated = copy.deepcopy(original)
    from backend.live_eth_quotes import STABLES
    official = STABLES["USDC"][2:].lower()
    wrong = "f"*40
    mutated["unsigned_transaction"]["data"] = (
        calldata[:10] + calldata[10:].replace(official, wrong, 1))
    with pytest.raises(ValueError):
        inspect(unsigned=mutated)

    mutated = copy.deepcopy(original)
    mutated["unsigned_transaction"]["data"] = "0xdeadbeef"
    with pytest.raises(ValueError):
        inspect(unsigned=mutated)


def test_owner_bound_allowance_calldata_not_user_spender():
    calldata=m._allowance_data(ETH,ROUTER)
    assert calldata[:10] == "0xdd62ed3e"
    assert calldata[10:74] == ETH[2:].lower().rjust(64,"0")
    assert calldata[74:] == ROUTER[2:].lower().rjust(64,"0")
    for owner,spender in ((ETH,"not_wallet"),("no",ROUTER)):
        with pytest.raises(ValueError):
            m._allowance_data(owner,spender)


def test_real_mainnet_rpc_readback_is_owner_bound_and_not_a_tx(monkeypatch):
    original = quote(asset="XRP",stable="USDC",amount=_AMOUNT)
    called = []
    async def route(did, **kwargs):
        assert did == "did:privy:owner"
        assert kwargs == {"asset":"XRP","side":"BUY",
                          "stable":"USDC","amount":_AMOUNT}
        return original
    async def rpc(client, *, method, params, req_id):
        called.append((method, params, req_id))
        replies = {
            "eth_chainId":"0x1",
            "eth_getBalance":hex(_ETH_WEI),
            "eth_call":"0x" + f"{_FUNDS:064x}",
            "eth_getTransactionCount":"0x2a",
            "eth_estimateGas":hex(_GAS),
            "eth_gasPrice":hex(_GAS_PRICE),
        }
        return replies[method]
    monkeypatch.setattr(m,"get_private_native_thornode_terms",route)
    monkeypatch.setattr(m,"_rpc",rpc)
    monkeypatch.setattr(m.time,"time",lambda:NOW)
    result=asyncio.run(m.preflight_original_native_eth_buy(
        "did:privy:owner",asset="XRP",stable="USDC",amount=_AMOUNT,
    ))
    assert result["asset"] == "XRP"
    assert result["chain_id_verified"] is True
    assert result["can_submit"] is False
    assert [x[0] for x in called] == [
        "eth_chainId","eth_getBalance","eth_call","eth_call",
        "eth_getTransactionCount","eth_estimateGas","eth_gasPrice",
    ]
    assert called[3][1][0]["to"].lower() == (
        __import__("backend.live_eth_quotes", fromlist=["STABLES"])
        .STABLES["USDC"].lower()
    )
    assert called[3][1][0]["data"] == m._allowance_data(ETH, ROUTER)



@pytest.mark.parametrize("field", [
    "source_wallet", "destination_wallet", "inbound_vault",
    "inbound_router", "source_asset", "destination_asset",
    "quote_expires_at", "memo", "amount",
])
def test_original_verified_quote_fields_cannot_be_replaced_in_preflight(field):
    original = quote()
    if field == "quote_expires_at":
        from datetime import timedelta
        original[field] += timedelta(seconds=1)
    elif field == "amount":
        original[field] = "49"
    elif field == "memo":
        original[field] = original[field].replace("0/1/0", "0/1/1")
    else:
        original[field] = "0x" + "f"*40
    with pytest.raises(ValueError):
        inspect(original_quote=original)


def test_original_unsigned_fingerprint_cannot_be_recomputed_with_wrong_route():
    draft = preview()
    changed = copy.deepcopy(draft)
    changed["router"] = "0x" + "f"*40
    changed["unsigned_transaction"]["to"] = changed["router"]
    with pytest.raises(ValueError):
        inspect(unsigned=changed)

    changed = copy.deepcopy(draft)
    changed["unsigned_transaction_sha256"] = "f"*64
    with pytest.raises(ValueError):
        inspect(unsigned=changed)


def test_original_input_amount_cannot_silently_round_on_rpc_readback():
    original = quote()
    original["amount"] = "50.0000001"
    with pytest.raises(ValueError):
        inspect(original_quote=original)

def test_native_buy_no_privy_signatures_or_erc20_approval_created():
    import inspect
    code=inspect.getsource(m.preflight_original_native_eth_buy)
    for action in ("send_transaction(", "approve(", "privy_rpc",
                   "sendrawtransaction", "private_key", "psycopg"):
        assert action not in code
