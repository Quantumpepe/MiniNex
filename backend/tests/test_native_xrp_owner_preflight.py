"""XRPL mainnet source preflight must use fresh validated ledger evidence.

All transactions remain unsigned and no Privy grant or chain submit occurs.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend import native_xrp_owner_preflight as m
from backend.tests.test_native_xrp_thor_payment_tx import (
    AMOUNT, OWNER, VAULT, NOW, quote, account,
)

def responses():
    return {
        "server_info": {
            "status": "success",
            "info": {
                "network_id": 0,
                "amendment_blocked": False,
                "validated_ledger": {
                    "seq": 100123, "age": 2,
                    "reserve_base_xrp": 1,
                    "reserve_inc_xrp": "0.2",
                },
            },
        },
        "fee": {
            "status": "success",
            "drops": {"open_ledger_fee": "12"},
        },
        "account_info": account()["result"],
    }


def test_original_xrpl_mainnet_server_fee_and_reserve_conversion():
    x = responses()
    result = m.inspect_xrpl_mainnet_server(x["server_info"], x["fee"])
    assert result == {
        "validated_ledger_index": 100123,
        "network_fee_drops": "12",
        "reserve_base_drops": "1000000",
        "reserve_increment_drops": "200000",
        "source_network_mainnet_verified": True,
    }


@pytest.mark.parametrize("change", [
    lambda r: r["server_info"]["info"].update(network_id=1),
    lambda r: r["server_info"]["info"].pop("network_id"),
    lambda r: r["server_info"]["info"].update(amendment_blocked=True),
    lambda r: r["server_info"]["info"]["validated_ledger"].update(seq=0),
    lambda r: r["server_info"]["info"]["validated_ledger"].update(age=31),
    lambda r: r["server_info"]["info"]["validated_ledger"].update(age="2"),
    lambda r: r["server_info"]["info"]["validated_ledger"].update(reserve_base_xrp="NaN"),
    lambda r: r["server_info"]["info"]["validated_ledger"].update(reserve_inc_xrp=-1),
    lambda r: r["fee"].update(status="error"),
    lambda r: r["fee"]["drops"].update(open_ledger_fee="0"),
    lambda r: r["fee"]["drops"].update(open_ledger_fee=12),
    lambda r: r["fee"]["drops"].update(open_ledger_fee="NaN"),
])
def test_wrong_network_stale_ledger_and_malformed_fee_fail_closed(change):
    x = responses()
    change(x)
    with pytest.raises(ValueError):
        m.inspect_xrpl_mainnet_server(x["server_info"], x["fee"])


@pytest.mark.parametrize("amount", [
    "1", "0.2", 1, 0, "0", "1.000001", "NaN",
    "-1", "Infinity", None, {}, "0.0000001",
])
def test_xrp_reserves_have_exact_drop_precision(amount):
    if amount in ("1", "0.2", 1, 0, "0", "1.000001"):
        result = m._xrp_drops(amount)
        assert result.isascii() and result.isdecimal()
    else:
        with pytest.raises(ValueError):
            m._xrp_drops(amount)


def _run(monkeypatch, data=None, now=NOW):
    answers = responses() if data is None else data
    calls = []

    async def private_terms(did, **params):
        assert did == "did:privy:xrp_original_owner"
        assert params == {
            "asset": "XRP", "side": "SELL",
            "stable": "USDC", "amount": AMOUNT,
        }
        return quote()

    async def rpc(client, method, params):
        calls.append((method, params))
        return answers[method]

    monkeypatch.setattr(m, "get_private_native_thornode_terms", private_terms)
    monkeypatch.setattr(m, "_xrp_rpc", rpc)
    monkeypatch.setattr(m.time, "time", lambda: now)
    result = asyncio.run(m.prepare_original_xrp_unsigned_owner_payment(
        "did:privy:xrp_original_owner", amount=AMOUNT))
    return result, calls


def test_native_xrp_original_source_compiles_only_unsigned_payment(monkeypatch):
    report, calls = _run(monkeypatch)
    assert report["mode"] == "ORIGINAL_XRP_SOURCE_UNSIGNED_MAINNET_PREFLIGHT"
    assert report["source_wallet"] == OWNER
    assert report["inbound_vault"] == VAULT
    assert report["source_amount_drops"] == "10123456"
    assert report["source_account_reserve_drops"] == "1400000"
    assert report["network_fee_drops"] == "12"
    assert report["validated_ledger_index"] == 100123
    assert report["account_sequence"] == 29
    assert [name for name, _ in calls] == [
        "server_info", "fee", "account_info",
    ]
    assert calls[-1][1] == [{
        "account": OWNER, "ledger_index": 100123, "strict": True,
    }]
    assert report["unsigned_payment"]["unsigned_payment"]["Flags"] == 0x80000000
    for key in (
        "original_privy_policy_verified", "original_order_claim_verified",
        "wallet_signed", "source_transaction_sent",
        "execution_enabled", "can_submit",
    ):
        assert report[key] is False


@pytest.mark.parametrize("mutation", [
    lambda d: d["account_info"].update(validated=False),
    lambda d: d["account_info"].update(ledger_index=100122),
    lambda d: d["account_info"]["account_data"].update(Account=VAULT),
    lambda d: d["account_info"]["account_data"].update(Sequence=0),
    lambda d: d["account_info"]["account_data"].update(Balance="100"),
    lambda d: d["account_info"]["account_data"].update(OwnerCount=-1),
    lambda d: d["server_info"]["info"].update(network_id=21338),
    lambda d: d["fee"]["drops"].update(open_ledger_fee="bad"),
])
def test_native_xrp_account_network_and_reserve_changes_never_sign(
    monkeypatch, mutation,
):
    data = responses()
    mutation(data)
    with pytest.raises((HTTPException, ValueError)):
        _run(monkeypatch, data=data)


def test_native_xrp_quote_revalidated_after_ledger_queries(monkeypatch):
    with pytest.raises((HTTPException, ValueError)):
        _run(monkeypatch, now=NOW + 121)


def test_native_xrp_preflight_does_not_submit_sign_or_broadcast():
    import inspect
    src = inspect.getsource(m.prepare_original_xrp_unsigned_owner_payment)
    for forbidden in (
        "sendrawtransaction", "submit(", "private_key", "signTransaction",
        "sign_and_submit", "psycopg", "PrivyClient",
    ):
        assert forbidden not in src
