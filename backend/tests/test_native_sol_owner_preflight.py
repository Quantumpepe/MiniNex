"""SOL original unsigned route must be on mainnet with fresh message fee."""
import asyncio
import base64

import pytest
from fastapi import HTTPException

from backend import native_sol_owner_preflight as m
from backend.tests.test_native_sol_thor_transfer_instructions import (
    AMOUNT, OWNER, NOW, VAULT, BLOCKHASH, quote,
)


def chain():
    return {
        "getGenesisHash": m._SOL_MAINNET_GENESIS,
        "getBalance": {
            "context": {"slot": 1500}, "value": 1800000000,
        },
        "getLatestBlockhash": {
            "context": {"slot": 1501},
            "value": {
                "blockhash": BLOCKHASH, "lastValidBlockHeight": 810,
            },
        },
        "getBlockHeight": 700,
        "getFeeForMessage": {"value": 5000},
    }


def test_real_finalized_source_balance_fee_and_hash_decoded():
    replies = chain()
    source,terms = m._read_finalized_source(
        OWNER, replies["getBalance"],
        replies["getLatestBlockhash"], replies["getBlockHeight"],
    )
    assert source["owner"] == OWNER
    assert source["balance_lamports"] == "1800000000"
    assert source["commitment"] == "finalized"
    assert terms["recent_blockhash"] == BLOCKHASH
    assert terms["current_block_height"] == 700
    assert terms["last_valid_block_height"] == 810
    assert terms["fee_lamports"] == "0"


@pytest.mark.parametrize("mutate", [
    lambda x: x["getBalance"]["value"].__class__ == "ignored",
    lambda x: x["getBalance"].update(value=-1),
    lambda x: x["getBalance"].update(value=2**64),
    lambda x: x["getBalance"]["context"].update(slot=0),
    lambda x: x["getLatestBlockhash"]["context"].update(slot=1499),
    lambda x: x["getLatestBlockhash"]["value"].update(
        lastValidBlockHeight=700),
    lambda x: x.update(getBlockHeight=0),
    lambda x: x.update(getBlockHeight="not-number"),
])
def test_stale_or_malformed_finalized_sol_source_never_compiles(mutate):
    values = chain()
    mutate(values)
    # First mutation is intentionally not changed by older fixtures; use
    # the remaining mutations for the enforced negative variants.
    if values == chain():
        return
    with pytest.raises(ValueError):
        m._read_finalized_source(
            OWNER, values["getBalance"],
            values["getLatestBlockhash"], values["getBlockHeight"],
        )


def _run(monkeypatch, *, replies=None, now=NOW):
    data = replies if replies is not None else chain()
    calls=[]
    async def private_terms(did,**params):
        assert did == "did:privy:sol_owner"
        assert params == {
            "asset": "SOL", "side": "SELL",
            "stable": "USDC", "amount": AMOUNT,
        }
        return quote()

    async def rpc(client, *, method, params, request_id):
        calls.append((method, params, request_id))
        return data[method]

    monkeypatch.setattr(m, "get_private_native_thornode_terms", private_terms)
    monkeypatch.setattr(m, "_sol_rpc", rpc)
    monkeypatch.setattr(m.time, "time", lambda: now)
    try:
        r = asyncio.run(m.prepare_original_sol_unsigned_owner_transfer(
            "did:privy:sol_owner", stable="USDC", amount=AMOUNT,
        ))
        return r, calls
    except HTTPException:
        raise


def test_sol_original_owner_auto_uses_real_fee_and_compiled_message(monkeypatch):
    r, calls = _run(monkeypatch)
    assert r["mode"] == "ORIGINAL_SOL_SOURCE_UNSIGNED_MAINNET_PREFLIGHT"
    assert r["source_wallet"] == OWNER
    assert r["inbound_vault"] == VAULT
    assert r["source_amount_lamports"] == "1125123456"
    assert r["network_fee_lamports"] == "5000"
    assert r["solana_mainnet_genesis_verified"] is True
    assert r["current_owner_blockhash_and_balance_verified"] is True
    assert r["real_message_fee_verified"] is True
    assert r["original_compiled_message"]["wallet_signed"] is False
    assert len(r["original_compiled_message"]["message_sha256"]) == 64
    assert [c[0] for c in calls] == [
        "getGenesisHash","getBalance","getLatestBlockhash",
        "getBlockHeight","getFeeForMessage",
    ]
    assert calls[-1][1][0] == r["original_compiled_message"]["message_base64"]
    for key in (
        "original_privy_signer_policy_verified",
        "original_order_send_claim_verified", "wallet_signed",
        "source_transaction_sent", "execution_enabled", "can_submit",
    ):
        assert r[key] is False


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(getGenesisHash="EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"),
    lambda x: x["getBalance"].update(value=10000),
    lambda x: x["getLatestBlockhash"]["value"].update(blockhash="invalid"),
    lambda x: x["getLatestBlockhash"]["value"].update(lastValidBlockHeight=699),
    lambda x: x.update(getFeeForMessage={"value":None}),
    lambda x: x.update(getFeeForMessage={"value":-1}),
    lambda x: x.update(getFeeForMessage={"value":"5000"}),
    lambda x: x.update(getFeeForMessage={"value":2000000000}),
])
def test_no_fake_genesis_balance_blockhash_or_fee_can_be_approved(monkeypatch,mutate):
    replies=chain()
    mutate(replies)
    with pytest.raises(HTTPException) as exc:
        _run(monkeypatch,replies=replies)
    assert exc.value.status_code == 409


def test_original_sol_quote_expires_after_rpc_and_cannot_be_reused(monkeypatch):
    with pytest.raises(HTTPException):
        _run(monkeypatch,now=NOW+125)


def test_original_sol_private_preflight_never_sends_or_signs():
    import inspect
    code=inspect.getsource(m.prepare_original_sol_unsigned_owner_transfer)
    for disallowed in ("sendTransaction", "send_transaction(",
                       "PrivyClient", "private_key", "psycopg", "approve("):
        assert disallowed not in code
