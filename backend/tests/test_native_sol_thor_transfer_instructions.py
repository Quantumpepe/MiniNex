"""Native SOL SELL exact SystemProgram+Memo instruction bytes, no signing."""
from datetime import datetime, timezone
import base64
import struct

import pytest

from backend import native_sol_thor_transfer_instructions as sol
from backend.manual_swap_live_quotes import THOR_ASSETS
ALPHABET="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58(data):
    n=int.from_bytes(data,"big")
    out=""
    while n:
        n,r=divmod(n,58)
        out=ALPHABET[r]+out
    return ALPHABET[0]*(len(data)-len(data.lstrip(b"\0")))+out

OWNER=b58(bytes([7])*32)
VAULT=b58(bytes([8])*32)
BLOCKHASH=b58(bytes([9])*32)
ETH="0x"+"a"*40
NOW=1803000000
AMOUNT="1.125123456"


def quote():
    return {
        "mode":"THORCHAIN_DESTINATION_BOUND_READ_ONLY",
        "source_asset":"SOL","destination_asset":"USDC",
        "amount":AMOUNT,
        "from_network":"solana-mainnet","to_network":"ethereum-mainnet",
        "source_wallet":OWNER,"inbound_vault":VAULT,
        "destination_wallet":ETH,
        "memo":"=:"+THOR_ASSETS["USDC"][0]+":"+ETH+"/"+OWNER+":0/1/0",
        "quote_expires_at":datetime.fromtimestamp(NOW+120,tz=timezone.utc),
        "original_vault_verified":True,
        "refund_path_verified":True,
        "required_chain_memo_verified":True,
        "exact_venue_fees_verified":True,
        "transaction_encoding_verified":False,
        "source_transaction_sent":False,
        "can_submit":False,"execution_enabled":False,
    }

def wallet():
    return {"owner":OWNER,"network":"solana-mainnet","commitment":"finalized",
            "slot":1500,"balance_lamports":"1800000000"}

def network():
    return {"recent_blockhash":BLOCKHASH,"context_slot":1501,
            "current_block_height":700,"last_valid_block_height":810,
            "fee_lamports":"5000"}

def build(**updates):
    params={"quote":quote(),"amount":AMOUNT,"now":NOW,
            "source_account":wallet(),"network_terms":network()}
    params.update(updates)
    return sol.build_unsigned_sol_thor_source_instructions(**params)


def test_exact_unsigned_sol_thorchain_system_transfer_and_memo():
    result=build()
    first,second=result["instructions"]
    assert result["source_amount_lamports"]=="1125123456"
    assert first["program_id"]=="11111111111111111111111111111111"
    assert first["accounts"]==[
        {"pubkey":OWNER,"signer":True,"writable":True},
        {"pubkey":VAULT,"signer":False,"writable":True},
    ]
    instruction=base64.b64decode(first["data_base64"])
    assert len(instruction)==12
    assert struct.unpack("<IQ",instruction)==(2,1125123456)
    assert second["program_id"]=="MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
    assert base64.b64decode(second["data_base64"]).decode("utf8")==quote()["memo"]
    assert result["recent_blockhash"]==BLOCKHASH
    assert result["source_fee_and_balance_checked"] is True
    assert result["current_privy_policy_verified"] is False
    assert result["message_compiled"] is False
    assert result["wallet_signed"] is False
    assert result["ready_to_submit"] is False
    assert result["execution_enabled"] is False
    assert result["can_submit"] is False
    assert len(result["unsigned_instructions_sha256"])==64


@pytest.mark.parametrize("change",[
    lambda q:q.update(source_wallet=VAULT),
    lambda q:q.update(inbound_vault="ETH"),
    lambda q:q.update(destination_wallet=OWNER),
    lambda q:q.update(destination_asset="BTC"),
    lambda q:q.update(from_network="ethereum-mainnet"),
    lambda q:q.update(amount="1"),
    lambda q:q.update(original_vault_verified=False),
    lambda q:q.update(refund_path_verified=False),
    lambda q:q.update(exact_venue_fees_verified=False),
    lambda q:q.update(required_chain_memo_verified=False),
    lambda q:q.update(source_transaction_sent=True),
    lambda q:q.update(transaction_encoding_verified=True),
    lambda q:q.update(can_submit=True),
    lambda q:q.update(execution_enabled=True),
    lambda q:q.update(memo="=:ETH.USDC:"+ETH+":0/1/0"),
    lambda q:q.update(memo="=:ETH.USDC:"+ETH+"/"+VAULT+":0/1/0"),
    lambda q:q.update(quote_expires_at=datetime.fromtimestamp(NOW-1,tz=timezone.utc)),
])
def test_swapped_vault_forged_route_or_refund_never_creates_instructions(change):
    q=quote()
    change(q)
    with pytest.raises(ValueError):
        build(quote=q)


@pytest.mark.parametrize("change",[
    lambda w:w.update(owner=VAULT),
    lambda w:w.update(network="ethereum-mainnet"),
    lambda w:w.update(commitment="processed"),
    lambda w:w.update(slot=-1),
    lambda w:w.update(balance_lamports="10000"),
    lambda w:w.update(balance_lamports="not-number"),
])
def test_source_balance_and_current_wallet_must_match(change):
    w=wallet()
    change(w)
    with pytest.raises(ValueError):
        build(source_account=w)


@pytest.mark.parametrize("change",[
    lambda n:n.update(recent_blockhash="nonsense"),
    lambda n:n.update(context_slot=1499),
    lambda n:n.update(last_valid_block_height=699),
    lambda n:n.update(current_block_height=810),
    lambda n:n.update(fee_lamports="NaN"),
    lambda n:n.update(fee_lamports="-1"),
    lambda n:n.update(fee_lamports="1000000000"),
])
def test_stale_blockhash_fee_or_excessive_cost_never_encodes(change):
    n=network()
    change(n)
    with pytest.raises(ValueError):
        build(network_terms=n)


@pytest.mark.parametrize("amount",[
    "0","-1","1.0000000001","NaN","1e4","",None,
])
def test_native_sol_payment_never_rounds_wrong_precision(amount):
    q=quote()
    q["amount"]=amount
    with pytest.raises(ValueError):
        build(quote=q,amount=amount)
