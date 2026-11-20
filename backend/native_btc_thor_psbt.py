"""BIP174 v0 unsigned PSBT for one original owner-funded BTC -> THORChain SELL.

This is a deterministic serialization only. It does not check external UTXO
finality, request Privy signing, reserve inputs, broadcast, or authorize any
live source transaction. The real executor must bind verified unspent outputs,
fresh user consent, policy and one-shot journal claim before signing.
"""
from __future__ import annotations

import base64
import hashlib
import re
import struct

from .native_btc_thor_outputs import (
    _mainnet_p2wpkh_script,
    preview_btc_thor_original_outputs,
)

_TXID = re.compile(r"^[a-fA-F0-9]{64}$")


def _compact_size(value: int) -> bytes:
    if type(value) is not int or not 0 <= value <= 0xffffffffffffffff:
        raise ValueError("Invalid Bitcoin CompactSize")
    if value < 253:
        return bytes((value,))
    if value <= 0xffff:
        return b"\xfd" + struct.pack("<H", value)
    if value <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", value)
    return b"\xff" + struct.pack("<Q", value)


def _psbt_record(key: bytes, value: bytes) -> bytes:
    if not key or not isinstance(value, bytes):
        raise ValueError("Invalid PSBT map field")
    return _compact_size(len(key)) + key + _compact_size(len(value)) + value


def compile_original_unsigned_btc_thor_psbt(
    quote: object, *, amount: str, now: int,
    owner_script_hex: str, vault_script_hex: str,
    input_utxos: object, fee_rate_sats_per_vbyte: int,
) -> dict:
    """Recompute quoted VOUTs and create a standard SegWit v0 PSBT container.

    Every supplied UTXO must state its original owner P2WPKH script for
    cross-checking before it is serialized as witness_utxo. Ownership and
    unspent state are NOT considered independently verified by this module.
    """
    if type(input_utxos) is not list or not input_utxos:
        raise ValueError("Original Bitcoin source inputs missing")
    plan = preview_btc_thor_original_outputs(
        quote, amount=amount, now=now,
        owner_script_hex=owner_script_hex, vault_script_hex=vault_script_hex,
        input_utxos=input_utxos,
        fee_rate_sats_per_vbyte=fee_rate_sats_per_vbyte,
    )
    owner_script = _mainnet_p2wpkh_script(plan["source_wallet"])
    if owner_script.hex() != owner_script_hex.lower():
        raise ValueError("Original Bitcoin source script changed")

    inputs = []
    seen = set()
    for utxo in input_utxos:
        if (type(utxo) is not dict
                or type(utxo.get("txid")) is not str
                or not _TXID.fullmatch(utxo["txid"])
                or type(utxo.get("vout")) is not int
                or not 0 <= utxo["vout"] <= 0xffffffff
                or type(utxo.get("value_sats")) is not int
                or not 0 < utxo["value_sats"] <= 21_000_000 * 100_000_000
                or type(utxo.get("script_pubkey_hex")) is not str
                or utxo["script_pubkey_hex"].lower() != owner_script.hex()):
            raise ValueError("Bitcoin witness UTXO not bound to owner script")
        identity = (utxo["txid"].lower(), utxo["vout"])
        if identity in seen:
            raise ValueError("Duplicate Bitcoin witness UTXO")
        seen.add(identity)
        inputs.append(utxo)

    outputs = plan["ordered_outputs"]
    if len(outputs) < 3 or [item.get("vout") for item in outputs] != list(range(len(outputs))):
        raise ValueError("Untrusted THORChain Bitcoin output order")
    total_out = 0
    out_bytes = bytearray()
    for item in outputs:
        sats = item.get("value_sats")
        hex_script = item.get("script_pubkey_hex")
        if (type(sats) is not int or not 0 <= sats < 21_000_000 * 100_000_000
                or type(hex_script) is not str):
            raise ValueError("Invalid Bitcoin script or payment value")
        try:
            script = bytes.fromhex(hex_script)
        except ValueError:
            raise ValueError("Invalid Bitcoin output script hex") from None
        if (len(script) > 10000
                or (item["vout"] == 0 and script !=
                    _mainnet_p2wpkh_script(plan["inbound_vault"]))
                or (item["vout"] == 1 and script != owner_script)):
            raise ValueError("Original Bitcoin THORChain payment script changed")
        out_bytes.extend(struct.pack("<Q", sats))
        out_bytes.extend(_compact_size(len(script)))
        out_bytes.extend(script)
        total_out += sats

    input_total = sum(item["value_sats"] for item in inputs)
    if (total_out + plan["estimated_network_fee_sats"] != input_total
            or input_total != plan["total_input_sats"]):
        raise ValueError("Bitcoin unsigned transaction fee balance mismatched")

    # BIP174 unsigned tx: no SegWit witness marker or witness data; all
    # scriptSigs empty. RBF sequence permits replacement before confirmation.
    tx = bytearray(struct.pack("<I", 2))
    tx.extend(_compact_size(len(inputs)))
    for item in inputs:
        tx.extend(bytes.fromhex(item["txid"])[::-1])
        tx.extend(struct.pack("<I", item["vout"]))
        tx.extend(b"\x00")
        tx.extend(struct.pack("<I", 0xfffffffd))
    tx.extend(_compact_size(len(outputs)))
    tx.extend(out_bytes)
    tx.extend(struct.pack("<I", 0))
    unsigned = bytes(tx)

    # PSBT magic; one unsigned-tx global; one witness_utxo map per input;
    # one empty output map per original ordered VOUT.
    psbt = bytearray(b"psbt\xff")
    psbt.extend(_psbt_record(b"\x00", unsigned))
    psbt.extend(b"\x00")
    for item in inputs:
        witness = (struct.pack("<Q", item["value_sats"])
                   + _compact_size(len(owner_script)) + owner_script)
        psbt.extend(_psbt_record(b"\x01", witness))
        psbt.extend(b"\x00")
    psbt.extend(b"\x00" * len(outputs))
    return {
        "mode": "ORIGINAL_BTC_THORCHAIN_UNSIGNED_PSBT_V0",
        "asset": "BTC", "side": "SELL",
        "source_wallet": plan["source_wallet"],
        "inbound_vault": plan["inbound_vault"],
        "destination_wallet": plan["destination_wallet"],
        "source_amount_sats": plan["source_amount_sats"],
        "miner_fee_sats": plan["estimated_network_fee_sats"],
        "original_output_digest_sha256": plan["unsigned_output_digest_sha256"],
        "unsigned_tx_hex": unsigned.hex(),
        "unsigned_txid": hashlib.sha256(hashlib.sha256(unsigned).digest()).digest()[::-1].hex(),
        "psbt_base64": base64.b64encode(psbt).decode("ascii"),
        "psbt_sha256": hashlib.sha256(psbt).hexdigest(),
        "input_count": len(inputs), "output_count": len(outputs),
        "psbt_created": True,
        "utxo_chain_ownership_checked": False,
        "utxo_unspent_state_checked": False,
        "current_privy_policy_verified": False,
        "owner_consent_verified": False,
        "original_order_claim_verified": False,
        "wallet_signed": False,
        "ready_to_submit": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
