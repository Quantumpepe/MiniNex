"""Original THORChain BTC SELL output order and 80-byte memo extension."""
from datetime import datetime,timezone
import pytest

from backend import native_btc_thor_outputs as btc
from backend.manual_swap_live_quotes import THOR_ASSETS

NOW=1803000000
SOURCE="bc1q"+"q"*38
VAULT="bc1q"+"p"*38
ETH="0x"+"a"*40
SOURCE_SCRIPT="0014"+"11"*20
VAULT_SCRIPT="0014"+"22"*20
UTXOS=[{"txid":"a"*64,"vout":0,"value_sats":5000000}]
AMOUNT="0.025"


def quote(memo=None):
    return {
        "mode":"THORCHAIN_DESTINATION_BOUND_READ_ONLY",
        "source_asset":"BTC","destination_asset":"USDC",
        "amount":AMOUNT,"from_network":"bitcoin-mainnet",
        "to_network":"ethereum-mainnet","source_wallet":SOURCE,
        "destination_wallet":ETH,"inbound_vault":VAULT,
        "memo":memo or "=:"+THOR_ASSETS["USDC"][0]+":"+ETH+"/"+SOURCE+":0/1/0",
        "quote_expires_at":datetime.fromtimestamp(NOW+120,tz=timezone.utc),
        "original_vault_verified":True,
        "required_chain_memo_verified":True,
        "refund_path_verified":True,
        "exact_venue_fees_verified":True,
        "transaction_encoding_verified":False,
        "source_transaction_sent":False,
        "execution_enabled":False,"can_submit":False,
    }


def preview(**changes):
    args={
        "quote":quote(),"amount":AMOUNT,"now":NOW,
        "owner_script_hex":SOURCE_SCRIPT,
        "vault_script_hex":VAULT_SCRIPT,
        "input_utxos":UTXOS,
        "fee_rate_sats_per_vbyte":5,
    }
    args.update(changes)
    return btc.preview_btc_thor_original_outputs(**args)


def test_exact_btc_vout0_vout1_opreturn_and_extended_chunks():
    report=preview()
    outputs=report["ordered_outputs"]
    assert [o["vout"] for o in outputs]==list(range(len(outputs)))
    assert outputs[0]["kind"]=="ASGARD_VAULT"
    assert outputs[0]["value_sats"]==2500000
    assert outputs[0]["script_pubkey_hex"]==VAULT_SCRIPT
    assert outputs[1]["kind"]=="OWNER_VIN0_CHANGE"
    assert outputs[1]["script_pubkey_hex"]==SOURCE_SCRIPT
    assert outputs[1]["value_sats"]>0
    assert outputs[2]["kind"]=="OP_RETURN"
    assert report["needs_extended_memo"] is True
    script=bytes.fromhex(outputs[2]["script_pubkey_hex"])
    assert script[:3]==b"\x6a\x4c\x50"
    assert script[3:]==quote()["memo"].encode()[:79]+b"^"
    extended=b"".join(bytes.fromhex(row["script_pubkey_hex"])[2:]
                      for row in outputs[3:])
    assert extended.rstrip(b"\0")==quote()["memo"].encode()[79:]
    assert all(row["value_sats"]==294 for row in outputs[3:])
    assert (report["total_input_sats"]==report["source_amount_sats"]+
            report["owner_change_sats"]+report["estimated_network_fee_sats"]+
            294*len(outputs[3:]))
    assert report["utxo_chain_ownership_checked"] is False
    assert report["current_privy_policy_verified"] is False
    assert report["wallet_signed"] is False
    assert report["psbt_created"] is False
    assert report["execution_enabled"] is False
    assert report["can_submit"] is False


def test_exact_short_btc_opreturn_with_zero_extension():
    memo="=:BTC.BTC:bc1qabcdefgh"
    outputs=btc.encode_thor_btc_memo_outputs(memo)
    assert len(outputs)==1
    assert outputs[0]["kind"]=="OP_RETURN"
    assert outputs[0]["value_sats"]==0
    assert bytes.fromhex(outputs[0]["script_pubkey_hex"])==(
        b"\x6a"+bytes([len(memo)])+memo.encode())


def test_at_80_and_81_bytes_extension_boundary():
    short="M"*80
    long="M"*81
    assert len(btc.encode_thor_btc_memo_outputs(short))==1
    chunks=btc.encode_thor_btc_memo_outputs(long)
    assert len(chunks)==2
    assert bytes.fromhex(chunks[0]["script_pubkey_hex"])==(
        b"\x6a\x4c\x50"+b"M"*79+b"^")
    assert chunks[1]["script_pubkey_hex"]=="0014"+("4d"+"00"*19)


def test_btc_extended_memo_above_protocol_vout_limit_rejected():
    with pytest.raises(ValueError):
        btc.encode_thor_btc_memo_outputs("M"*230)


@pytest.mark.parametrize("mutate",[
    lambda q:q.update(source_wallet=VAULT),
    lambda q:q.update(inbound_vault="bc1p"+"z"*58),
    lambda q:q.update(destination_wallet=SOURCE),
    lambda q:q.update(destination_asset="XRP"),
    lambda q:q.update(amount="0.01"),
    lambda q:q.update(memo="=:ETH.USDC:"+ETH+":0/1/0"),
    lambda q:q.update(memo="=:ETH.USDC:"+ETH+"/"+VAULT+":0/1/0"),
    lambda q:q.update(original_vault_verified=False),
    lambda q:q.update(refund_path_verified=False),
    lambda q:q.update(required_chain_memo_verified=False),
    lambda q:q.update(exact_venue_fees_verified=False),
    lambda q:q.update(transaction_encoding_verified=True),
    lambda q:q.update(source_transaction_sent=True),
    lambda q:q.update(can_submit=True),
    lambda q:q.update(execution_enabled=True),
    lambda q:q.update(quote_expires_at=datetime.fromtimestamp(NOW-1,tz=timezone.utc)),
])
def test_btc_vault_refund_and_expired_quote_never_create_unsigned_plan(mutate):
    q=quote()
    mutate(q)
    with pytest.raises(ValueError):
        preview(quote=q)


@pytest.mark.parametrize("inputs,fee",[
    ([{"txid":"a"*64,"vout":0,"value_sats":1000}],5),
    ([{"txid":"a"*64,"vout":0,"value_sats":5000000},
      {"txid":"a"*64,"vout":0,"value_sats":5000000}],5),
    ([{"txid":"fake","vout":0,"value_sats":5000000}],5),
    ([{"txid":"a"*64,"vout":-1,"value_sats":5000000}],5),
    (UTXOS,-1),
    (UTXOS,0),
])
def test_btc_source_amount_owner_change_and_exact_utxos_required(inputs,fee):
    with pytest.raises(ValueError):
        preview(input_utxos=inputs,fee_rate_sats_per_vbyte=fee)


def test_no_wallet_signing_broadcast_or_hidden_key_in_btc_output_builder():
    import inspect
    c=inspect.getsource(btc.preview_btc_thor_original_outputs)
    for forbidden in ["PrivyClient","psycopg","httpx","sendRawTransaction",
                      "bitcoin.sign","wif","seed_phrase","private_key"]:
        assert forbidden not in c
