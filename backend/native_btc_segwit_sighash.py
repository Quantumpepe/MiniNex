"""BIP143 SIGHASH_ALL messages for exact original-owner Bitcoin PSBT inputs.

Privy Bitcoin SegWit uses raw secp256k1 signing per input; do not ask it to
sign arbitrary user bytes. First independently recompute every PSBT witness
UTXO, original input order, source address and full output transaction. This
module returns only 32-byte signature digests, never signatures, signatures
requests, a final transaction, or broadcast rights.
"""
from __future__ import annotations

import base64
import hashlib
import re
import struct

from .native_btc_thor_outputs import _mainnet_p2wpkh_script

_TXID = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_SATS = 21_000_000 * 100_000_000


def _hash256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _compact(data: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(data):
        raise ValueError("Unsigned Bitcoin transaction truncated")
    first = data[pos]
    pos += 1
    if first < 253:
        return first, pos
    size, format_, minimum = (
        (2, "<H", 253) if first == 253 else
        (4, "<I", 65536) if first == 254 else
        (8, "<Q", 4294967296)
    )
    if pos + size > len(data):
        raise ValueError("Bitcoin CompactSize exceeds transaction")
    n = struct.unpack_from(format_, data, pos)[0]
    if n < minimum:
        raise ValueError("Noncanonical Bitcoin CompactSize")
    return n, pos + size


def _parse_original_unsigned_tx(raw: bytes) -> dict:
    if type(raw) is not bytes or not 10 <= len(raw) <= 500000:
        raise ValueError("Original Bitcoin unsigned tx unavailable")
    if raw[:4] != struct.pack("<I", 2):
        raise ValueError("Unsupported original Bitcoin tx version")
    pos = 4
    nin, pos = _compact(raw, pos)
    if not 1 <= nin <= 256:
        raise ValueError("Original Bitcoin input count invalid")
    inputs = []
    for _ in range(nin):
        if pos + 36 > len(raw):
            raise ValueError("Bitcoin previous outpoint truncated")
        outpoint = raw[pos:pos + 36]
        pos += 36
        script_length, pos = _compact(raw, pos)
        if script_length != 0 or pos + 4 > len(raw):
            raise ValueError("Signed or malformed Bitcoin input script")
        sequence = raw[pos:pos + 4]
        pos += 4
        if sequence != struct.pack("<I", 0xfffffffd):
            raise ValueError("Original Bitcoin input sequence changed")
        inputs.append((outpoint, sequence))
    nout, pos = _compact(raw, pos)
    if not 3 <= nout <= 10:
        raise ValueError("Original THORChain output order missing")
    outputs = []
    for _ in range(nout):
        start = pos
        if pos + 8 > len(raw):
            raise ValueError("Bitcoin output value missing")
        value = struct.unpack_from("<Q", raw, pos)[0]
        if value > _MAX_SATS:
            raise ValueError("Bitcoin output amount invalid")
        pos += 8
        script_len, pos = _compact(raw, pos)
        if script_len > 10000 or pos + script_len > len(raw):
            raise ValueError("Bitcoin output script malformed")
        pos += script_len
        outputs.append(raw[start:pos])
    if pos + 4 != len(raw) or raw[pos:] != b"\x00\x00\x00\x00":
        raise ValueError("Bitcoin locktime or trailing transaction bytes invalid")
    return {"inputs": inputs, "outputs": outputs}


def _check_original_psbt_maps(
    psbt: bytes, unsigned: bytes, expected_utxos: list[dict],
    owner_script: bytes, output_count: int,
) -> None:
    if not psbt.startswith(b"psbt\xff"):
        raise ValueError("Invalid original Bitcoin PSBT magic")
    pos = 5
    size, pos = _compact(psbt, pos)
    if size != 1 or psbt[pos:pos + 1] != b"\x00":
        raise ValueError("Original PSBT unsigned transaction missing")
    pos += 1
    length, pos = _compact(psbt, pos)
    if psbt[pos:pos + length] != unsigned:
        raise ValueError("Bitcoin PSBT unsigned tx and digest differ")
    pos += length
    if psbt[pos:pos + 1] != b"\x00":
        raise ValueError("Original Bitcoin PSBT global map not closed")
    pos += 1
    for row in expected_utxos:
        size, pos = _compact(psbt, pos)
        if size != 1 or psbt[pos:pos + 1] != b"\x01":
            raise ValueError("Missing original Bitcoin witness UTXO")
        pos += 1
        length, pos = _compact(psbt, pos)
        witness = (
            struct.pack("<Q", row["value_sats"])
            + bytes((len(owner_script),)) + owner_script
        )
        if length != len(witness) or psbt[pos:pos + length] != witness:
            raise ValueError("Bitcoin PSBT witness UTXO value or script changed")
        pos += length
        if psbt[pos:pos + 1] != b"\x00":
            raise ValueError("Original Bitcoin PSBT input map not closed")
        pos += 1
    if psbt[pos:] != b"\x00" * output_count:
        raise ValueError("Bitcoin PSBT output maps or trailing data changed")


def build_original_btc_segwit_v0_sighashes(
    psbt: object, *, original_utxos: object,
) -> dict:
    """Compute original-owner witness-v0 input hashes after PSBT byte check."""
    if (type(psbt) is not dict
            or psbt.get("mode") != "ORIGINAL_BTC_THORCHAIN_UNSIGNED_PSBT_V0"
            or psbt.get("asset") != "BTC" or psbt.get("side") != "SELL"
            or psbt.get("psbt_created") is not True
            or psbt.get("wallet_signed") is not False
            or psbt.get("ready_to_submit") is not False
            or psbt.get("execution_enabled") is not False
            or psbt.get("can_submit") is not False
            or psbt.get("source_transaction_sent") is not False
            or type(original_utxos) is not list or not original_utxos
            or type(psbt.get("unsigned_tx_hex")) is not str
            or type(psbt.get("psbt_base64")) is not str):
        raise ValueError("Unsigned original Bitcoin owner context invalid")
    owner_script = _mainnet_p2wpkh_script(psbt.get("source_wallet"))
    vault_script = _mainnet_p2wpkh_script(psbt.get("inbound_vault"))
    if owner_script == vault_script:
        raise ValueError("Bitcoin original owner cannot equal inbound vault")
    try:
        unsigned = bytes.fromhex(psbt["unsigned_tx_hex"])
        envelope = base64.b64decode(psbt["psbt_base64"], validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Bitcoin original PSBT encoding invalid") from None
    if (type(psbt.get("psbt_sha256")) is not str
            or hashlib.sha256(envelope).hexdigest() != psbt["psbt_sha256"]
            or type(psbt.get("unsigned_txid")) is not str
            or _hash256(unsigned)[::-1].hex() != psbt["unsigned_txid"]):
        raise ValueError("Original PSBT or unsigned tx fingerprint changed")

    parsed = _parse_original_unsigned_tx(unsigned)
    input_rows = parsed["inputs"]
    outputs = parsed["outputs"]
    if (len(original_utxos) != len(input_rows)
            or psbt.get("input_count") != len(input_rows)
            or psbt.get("output_count") != len(outputs)):
        raise ValueError("Bitcoin source outpoint list changed")
    expected = []
    for (outpoint, _), row in zip(input_rows, original_utxos):
        if (type(row) is not dict
                or type(row.get("txid")) is not str
                or not _TXID.fullmatch(row["txid"])
                or type(row.get("vout")) is not int
                or not 0 <= row["vout"] <= 0xffffffff
                or type(row.get("value_sats")) is not int
                or not 0 < row["value_sats"] <= _MAX_SATS
                or row.get("script_pubkey_hex") != owner_script.hex()
                or outpoint != (
                    bytes.fromhex(row["txid"])[::-1]
                    + struct.pack("<I", row["vout"])
                )):
            raise ValueError("Original BTC signing outpoint or value mismatched")
        expected.append(row)
    _check_original_psbt_maps(envelope, unsigned, expected, owner_script, len(outputs))
    if len(outputs[0]) != 31 or outputs[0][9:] != vault_script:
        # P2WPKH output: 8-byte value + 1-byte script length + 22-byte script.
        raise ValueError("Original BTC VOUT0 is not the quoted Asgard vault")
    if len(outputs[1]) != 31 or outputs[1][9:] != owner_script:
        raise ValueError("Original BTC VOUT1 is not owner change")

    hash_prevouts = _hash256(b"".join(outpoint for outpoint, _ in input_rows))
    hash_sequence = _hash256(b"".join(seq for _, seq in input_rows))
    hash_outputs = _hash256(b"".join(outputs))
    program = owner_script[2:]
    script_code = b"\x19\x76\xa9\x14" + program + b"\x88\xac"
    messages = []
    for index, ((outpoint, sequence), row) in enumerate(
        zip(input_rows, expected)
    ):
        preimage = (
            struct.pack("<I", 2) + hash_prevouts + hash_sequence
            + outpoint + script_code
            + struct.pack("<Q", row["value_sats"]) + sequence
            + hash_outputs + struct.pack("<I", 0)
            + struct.pack("<I", 1)  # SIGHASH_ALL
        )
        messages.append({
            "input_index": index,
            "prevout_txid": row["txid"].lower(),
            "prevout_vout": row["vout"],
            "value_sats": row["value_sats"],
            "bip143_sighash_all_hex": _hash256(preimage).hex(),
        })
    return {
        "mode": "BTC_ORIGINAL_OWNER_SEGWIT_V0_SIGHASHES_ONLY",
        "asset": "BTC",
        "source_wallet": psbt["source_wallet"],
        "original_unsigned_txid": psbt["unsigned_txid"],
        "original_psbt_sha256": psbt["psbt_sha256"],
        "sighash_type": 1,
        "signature_digests": messages,
        "input_count": len(messages),
        "signature_requests_performed": False,
        "privy_signer_policy_verified": False,
        "source_outpoint_lock_acquired": False,
        "signature_applied": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
