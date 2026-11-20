"""Unsigned THORChain Bitcoin SELL outputs, including extended UTXO memo.

The THORChain protocol inspects ordered Bitcoin VOUTs:
  VOUT0: fresh Asgard vault credit
  VOUT1: change to the original input (VIN0) owner
  VOUT2: OP_RETURN, full memo up to 80 bytes, else first 79 + '^'
  VOUT3+: 20-byte P2WPKH script payloads for remaining memo (294 sats).
This is an encoding and fee preview, NOT a Bitcoin transaction builder,
Privy signer, funding instruction, UTXO claim or executable transaction.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .manual_swap_live_quotes import THOR_ASSETS

_COINS=frozenset(("USDC","USDT"))
_MONEY=re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_MAX_OUTPUTS=10
# THORChain documented minimum P2WPKH extension output on Bitcoin mainnet.
_EXTENSION_DUST_SATS=294
# Conservative fee calculation includes P2WPKH serialized virtual size.
_P2WPKH_INPUT_VBYTES=69
_TX_OVERHEAD_VBYTES=12
_MIN_VAULT_SATS=10000
_MIN_CHANGE_SATS=294
_BECH32_ALPHABET="qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATORS=(0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3)


def _bech32_polymod(values:list[int])->int:
    chk=1
    for value in values:
        top=chk>>25
        chk=((chk&0x1ffffff)<<5)^value
        for i,generator in enumerate(_BECH32_GENERATORS):
            if (top>>i)&1:
                chk^=generator
    return chk


def _mainnet_p2wpkh_script(address:object)->bytes:
    """Decode BIP173 mainnet v0 P2WPKH including its full checksum.

    A caller-supplied script is not proof of the quoted Asgard destination:
    derive the script from the quoted address before constructing any VOUT.
    """
    if (type(address) is not str or len(address)!=42
            or not address.startswith("bc1")
            or address!=address.lower()):
        raise ValueError("Expected original Bitcoin mainnet P2WPKH address")
    values=[_BECH32_ALPHABET.find(ch) for ch in address[3:]]
    if (-1 in values or values[0]!=0
            or _bech32_polymod([3,3,0,2,3]+values)!=1):
        raise ValueError("Bitcoin P2WPKH address checksum or version invalid")
    words=values[1:-6]
    if len(words)!=32:
        raise ValueError("Bitcoin witness program must be exactly 20 bytes")
    acc=bits=0
    program=bytearray()
    for word in words:
        acc=(acc<<5)|word
        bits+=5
        if bits>=8:
            bits-=8
            program.append((acc>>bits)&255)
            acc&=(1<<bits)-1
    if bits>=5 or acc!=0 or len(program)!=20:
        raise ValueError("Invalid Bitcoin SegWit witness padding")
    return b"\\x00\\x14"+bytes(program)


def _parse_sats(value:object)->int:
    if type(value) is not str or len(value)>48 or not _MONEY.fullmatch(value):
        raise ValueError("Invalid exact original BTC source amount")
    try:
        number=Decimal(value)*Decimal(10**8)
    except (ValueError,InvalidOperation):
        raise ValueError("BTC source amount cannot be encoded") from None
    if (not number.is_finite() or number!=number.to_integral_value()
            or number<=0 or number>21_000_000*10**8):
        raise ValueError("BTC source amount out of satoshi precision")
    return int(number)


def encode_thor_btc_memo_outputs(memo:object)->list[dict]:
    """Encode ordered VOUT2+ scripts; no untrusted addresses or wallet auth."""
    if type(memo) is not str or not memo or any(
            ord(char)>127 for char in memo):
        raise ValueError("THORChain Bitcoin memo must be printable ASCII")
    encoded=memo.encode("ascii")
    if (len(encoded)>4096
            or any(ch<0x20 or ch>0x7e for ch in encoded)):
        raise ValueError("Unsupported original BTC memo characters")
    chunks=[]
    if len(encoded)<=80:
        main=encoded
    else:
        main=encoded[:79]+b"^"
        remainder=encoded[79:]
        for offset in range(0,len(remainder),20):
            part=remainder[offset:offset+20].ljust(20,b"\x00")
            chunks.append(part)
    # The standard 80-byte OP_RETURN uses OP_PUSHDATA1, not an invalid
    # direct push opcode for byte lengths above 75.
    op_script=(b"\x6a"+(bytes([len(main)]) if len(main)<=75
                 else b"\x4c"+bytes([len(main)]))+main)
    outputs=[{"kind":"OP_RETURN","value_sats":0,
              "script_pubkey_hex":op_script.hex()}]
    for index,part in enumerate(chunks):
        outputs.append({"kind":"EXTENDED_MEMO",
                        "extension_index":index,
                        "value_sats":_EXTENSION_DUST_SATS,
                        "script_pubkey_hex":(b"\x00\x14"+part).hex()})
    if len(outputs)+2>_MAX_OUTPUTS:
        raise ValueError("THORChain Bitcoin memo exceeds 10-output protocol limit")
    return outputs


def preview_btc_thor_original_outputs(
    quote:object,*,amount:str,now:int,
    owner_script_hex:str,vault_script_hex:str,
    input_utxos:object,fee_rate_sats_per_vbyte:int,
)->dict:
    """Encode exact ordered Bitcoin VOUTs with independently supplied scripts.

    Input UTXOs must first be verified against original wallet chain by the
    future executor. This pure preview cannot claim a real UTXO reservation.
    """
    if (type(quote) is not dict or type(now) is not int or now<=0
            or type(fee_rate_sats_per_vbyte) is not int
            or not 0<fee_rate_sats_per_vbyte<=1_000_000
            or type(input_utxos) is not list or not input_utxos):
        raise ValueError("Untrusted original BTC source preflight")
    sats=_parse_sats(amount)
    owner=quote.get("source_wallet")
    vault=quote.get("inbound_vault")
    target=quote.get("destination_wallet")
    memo=quote.get("memo")
    expiry=quote.get("quote_expires_at")
    if (quote.get("mode")!="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or quote.get("source_asset")!="BTC"
            or quote.get("destination_asset") not in _COINS
            or quote.get("from_network")!="bitcoin-mainnet"
            or quote.get("to_network")!="ethereum-mainnet"
            or quote.get("amount")!=amount
            or type(owner) is not str
            or type(vault) is not str
            or owner==vault or type(target) is not str
            or not re.fullmatch(r"0x[0-9a-fA-F]{40}",target)
            or not isinstance(expiry,datetime) or expiry.tzinfo is None
            or not now<expiry.timestamp()<=now+600
            or quote.get("original_vault_verified") is not True
            or quote.get("required_chain_memo_verified") is not True
            or quote.get("refund_path_verified") is not True
            or quote.get("exact_venue_fees_verified") is not True
            or quote.get("transaction_encoding_verified") is not False
            or quote.get("source_transaction_sent") is not False
            or quote.get("execution_enabled") is not False
            or quote.get("can_submit") is not False):
        raise ValueError("Unverified original BTC swap and refund owner")
    parts=memo.split(":") if type(memo) is str else []
    destinations=parts[2].split("/") if len(parts)>=3 else []
    if (len(parts)<3 or parts[0].upper() not in ("=","S","SWAP")
            or parts[1].upper()!=THOR_ASSETS[quote["destination_asset"]][0].upper()
            or len(destinations)!=2
            or destinations[0].lower()!=target.lower()
            or destinations[1]!=owner):
        raise ValueError("Original BTC memo destination/refund mismatch")

    def script(value:object)->bytes:
        if (type(value) is not str or not re.fullmatch(r"[a-fA-F0-9]{44}",value)
                or not value.lower().startswith("0014")):
            raise ValueError("Original owner and vault must use P2WPKH")
        return bytes.fromhex(value)
    owner_script=script(owner_script_hex)
    vault_script=script(vault_script_hex)
    if (owner_script!=_mainnet_p2wpkh_script(owner)
            or vault_script!=_mainnet_p2wpkh_script(vault)):
        raise ValueError("BTC output script differs from quoted owner or Asgard vault")
    if owner_script==vault_script:
        raise ValueError("Cannot send THORChain BTC to the originating wallet")
    seen=set()
    total=0
    for unspent in input_utxos:
        if (type(unspent) is not dict
                or type(unspent.get("txid")) is not str
                or not re.fullmatch(r"[0-9a-fA-F]{64}",unspent["txid"])
                or type(unspent.get("vout")) is not int
                or not 0<=unspent["vout"]<=0xffffffff
                or type(unspent.get("value_sats")) is not int
                or not 0<unspent["value_sats"]<21_000_000*10**8):
            raise ValueError("Original BTC UTXO identity unavailable")
        key=(unspent["txid"].lower(),unspent["vout"])
        if key in seen:
            raise ValueError("Duplicate Bitcoin UTXO claimed in source plan")
        seen.add(key)
        total+=unspent["value_sats"]
    if sats<_MIN_VAULT_SATS:
        raise ValueError("THORChain BTC vault source amount below network dust")
    memo_outputs=encode_thor_btc_memo_outputs(memo)
    def vbytes(raw:bytes)->int:
        return 8+(1 if len(raw)<253 else 3)+len(raw)
    extended=sum(o["value_sats"] for o in memo_outputs)
    all_scripts=[vault_script,owner_script]+[
        bytes.fromhex(row["script_pubkey_hex"]) for row in memo_outputs]
    # Deliberate conservative fee headroom: 8 virtual bytes in addition to
    # the P2WPKH size estimate. Never exceed the user's UTXO holdings.
    weight=_TX_OVERHEAD_VBYTES+_P2WPKH_INPUT_VBYTES*len(input_utxos)
    weight+=sum(vbytes(s) for s in all_scripts)+8
    miner_fee=weight*fee_rate_sats_per_vbyte
    change=total-sats-extended-miner_fee
    if change<_MIN_CHANGE_SATS:
        raise ValueError("Cannot preserve original VIN0 change/refund wallet")
    ordered=[
        {"vout":0,"kind":"ASGARD_VAULT","value_sats":sats,
         "script_pubkey_hex":vault_script.hex()},
        {"vout":1,"kind":"OWNER_VIN0_CHANGE","value_sats":change,
         "script_pubkey_hex":owner_script.hex()},
    ]
    ordered.extend({**row,"vout":i+2}
                   for i,row in enumerate(memo_outputs))
    fingerprint=hashlib.sha256(json.dumps(
        [owner,vault,target,amount,expiry.isoformat(),input_utxos,ordered,
         fee_rate_sats_per_vbyte],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode":"ORIGINAL_BTC_THORCHAIN_ORDERED_OUTPUT_PREVIEW",
        "asset":"BTC","side":"SELL","source_wallet":owner,
        "inbound_vault":vault,"destination_wallet":target,
        "ordered_outputs":ordered,
        "source_amount_sats":sats,
        "total_input_sats":total,
        "owner_change_sats":change,
        "estimated_network_fee_sats":miner_fee,
        "fee_rate_sats_per_vbyte":fee_rate_sats_per_vbyte,
        "needs_extended_memo":len(memo.encode("ascii"))>80,
        "unsigned_output_digest_sha256":fingerprint,
        "utxo_chain_ownership_checked":False,
        "current_privy_policy_verified":False,
        "wallet_signed":False,"psbt_created":False,
        "source_transaction_sent":False,
        "execution_enabled":False,"can_submit":False,
    }
