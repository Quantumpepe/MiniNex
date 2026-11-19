"""Wire-level SOL legacy-message tests, independent from message constructor."""
from datetime import datetime, timezone
import base64
import hashlib
import struct

import pytest

from backend.native_sol_compiled_message import compile_original_unsigned_sol_message
from backend.manual_swap_live_quotes import THOR_ASSETS

ABC = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(raw):
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = ABC[r] + out
    return ABC[0] * (len(raw) - len(raw.lstrip(b"\0"))) + out


OWNER, VAULT, BLOCKHASH = (b58(bytes([x]) * 32) for x in (7, 8, 9))
ETH, NOW, AMOUNT = "0x" + "a" * 40, 1803000000, "1.125123456"


def params():
    return {
        "quote": {
            "mode": "THORCHAIN_DESTINATION_BOUND_READ_ONLY",
            "source_asset": "SOL", "destination_asset": "USDC",
            "amount": AMOUNT,
            "from_network": "solana-mainnet", "to_network": "ethereum-mainnet",
            "source_wallet": OWNER, "inbound_vault": VAULT,
            "destination_wallet": ETH,
            "memo": "=:" + THOR_ASSETS["USDC"][0] + ":" + ETH + "/" + OWNER + ":0/1/0",
            "quote_expires_at": datetime.fromtimestamp(NOW + 120, tz=timezone.utc),
            "original_vault_verified": True, "refund_path_verified": True,
            "required_chain_memo_verified": True,
            "exact_venue_fees_verified": True,
            "transaction_encoding_verified": False,
            "source_transaction_sent": False, "can_submit": False,
            "execution_enabled": False,
        },
        "amount": AMOUNT, "now": NOW,
        "source_account": {
            "owner": OWNER, "network": "solana-mainnet",
            "commitment": "finalized", "slot": 1500,
            "balance_lamports": "1800000000",
        },
        "network_terms": {
            "recent_blockhash": BLOCKHASH, "context_slot": 1501,
            "current_block_height": 700, "last_valid_block_height": 810,
            "fee_lamports": "5000",
        },
    }


def vec(data, i):
    result, shift = 0, 0
    while True:
        value = data[i]
        i += 1
        result |= (value & 127) << shift
        if value < 128:
            return result, i
        shift += 7
        assert shift <= 14


def test_exact_owner_signed_transfer_and_memo_message():
    p = params()
    result = compile_original_unsigned_sol_message(**p)
    raw = base64.b64decode(result["message_base64"], validate=True)
    assert result["message_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["message_compiled"] is True
    assert result["required_signers"] == [OWNER]
    assert result["wallet_signed"] is False
    assert result["privy_signer_grant_verified"] is False
    assert result["can_submit"] is False
    assert raw[:3] == bytes((1, 0, 2))
    count, offset = vec(raw, 3)
    assert count == 4
    assert raw[offset:offset + 32] == bytes([7]) * 32
    assert raw[offset + 32:offset + 64] == bytes([8]) * 32
    offset += 4 * 32
    assert raw[offset:offset + 32] == bytes([9]) * 32
    offset += 32
    n, offset = vec(raw, offset)
    assert n == 2
    assert raw[offset] == 2
    n, offset = vec(raw, offset + 1)
    assert n == 2 and raw[offset:offset + 2] == bytes((0, 1))
    n, offset = vec(raw, offset + 2)
    assert n == 12
    assert struct.unpack("<IQ", raw[offset:offset + 12]) == (2, 1125123456)
    offset += 12
    assert raw[offset] == 3
    n, offset = vec(raw, offset + 1)
    assert n == 1 and raw[offset] == 0
    n, offset = vec(raw, offset + 1)
    memo = p["quote"]["memo"].encode("utf-8")
    assert n == len(memo) and raw[offset:] == memo


@pytest.mark.parametrize("mutate", [
    lambda p: p["quote"].update(inbound_vault=OWNER),
    lambda p: p["quote"].update(memo="=:ETH.USDC:" + ETH + "/" + VAULT),
    lambda p: p["quote"].update(source_transaction_sent=True),
    lambda p: p["quote"].update(execution_enabled=True),
    lambda p: p["quote"].update(quote_expires_at=datetime.fromtimestamp(
        NOW - 1, tz=timezone.utc)),
    lambda p: p["source_account"].update(owner=VAULT),
    lambda p: p["source_account"].update(balance_lamports="1"),
    lambda p: p["network_terms"].update(recent_blockhash="wrong"),
    lambda p: p["network_terms"].update(current_block_height=810),
])
def test_reject_changed_wallet_memo_spend_or_expired_blockhash(mutate):
    p = params()
    mutate(p)
    with pytest.raises(ValueError):
        compile_original_unsigned_sol_message(**p)


def test_repeated_compilation_is_stable_and_never_signs():
    one = compile_original_unsigned_sol_message(**params())
    two = compile_original_unsigned_sol_message(**params())
    assert one == two
    assert "signature" not in one
    assert "signed_transaction" not in one
    assert one["ready_to_submit"] is False
