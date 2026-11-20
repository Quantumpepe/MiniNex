"""XRP native SELL builds an actual deterministic unsigned XRPL Payment.

Original owner wallet, Asgard vault, quote TTL, memo/refund, ledger sequence,
network fee and protocol reserve must agree. Never sign, submit or charge.
"""
from datetime import datetime,timezone

import pytest

from backend import native_xrp_thor_payment_tx as tx
from backend.native_coin_balances import _RIPPLE,_checksum
from backend.manual_swap_live_quotes import THOR_ASSETS

DID="did:privy:xrp_original_owner"
ALPHABET="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def base58(data,alphabet=_RIPPLE):
    n=int.from_bytes(data,"big")
    result=""
    while n:
        n,r=divmod(n,58)
        result=alphabet[r]+result
    return alphabet[0]*(len(data)-len(data.lstrip(b"\0")))+result

def xrp(bytes20):
    raw=b"\0"+bytes([bytes20])*20
    return base58(raw+_checksum(raw))
OWNER=xrp(3)
VAULT=xrp(7)
ETH="0x"+"a"*40
NOW=1803000000
AMOUNT="10.123456"


def quote():
    return {
        "mode":"THORCHAIN_DESTINATION_BOUND_READ_ONLY",
        "source_asset":"XRP","destination_asset":"USDC",
        "amount":AMOUNT,"from_network":"xrpl-mainnet",
        "to_network":"ethereum-mainnet",
        "source_wallet":OWNER,"destination_wallet":ETH,
        "inbound_vault":VAULT,"inbound_router":None,
        "memo":"=:"+THOR_ASSETS["USDC"][0]+":"+ETH+"/"+OWNER+":0/1/0",
        "quote_expires_at":datetime.fromtimestamp(NOW+120,tz=timezone.utc),
        "original_vault_verified":True,"required_chain_memo_verified":True,
        "refund_path_verified":True,"exact_venue_fees_verified":True,
        "transaction_encoding_verified":False,
        "source_transaction_sent":False,
        "execution_enabled":False,"can_submit":False,
    }

def account():
    return {"result":{
        "status":"success","validated":True,"ledger_index":100123,
        "account_data":{
            "Account":OWNER,"Sequence":29,"OwnerCount":2,
            "Balance":"25000000",
        },
    }}

def network():
    return {
        "validated_ledger_index":100123,
        "network_fee_drops":"12",
        "reserve_base_drops":"1000000",
        "reserve_increment_drops":"200000",
    }

def build(**overrides):
    args={"quote":quote(),"amount":AMOUNT,"now":NOW,
          "account_info":account(),"network_terms":network()}
    args.update(overrides)
    return tx.build_original_unsigned_xrp_thor_payment(**args)


def test_exact_xrp_thorchain_unsigned_original_payment_and_reserve():
    result=build()
    payment=result["unsigned_payment"]
    assert payment["Account"]==OWNER
    assert payment["Destination"]==VAULT
    assert payment["Amount"]=="10123456"
    assert payment["Fee"]=="12"
    assert payment["Sequence"]==29
    assert payment["LastLedgerSequence"]==100143
    assert "DestinationTag" not in payment
    assert bytes.fromhex(payment["Memos"][0]["Memo"]["MemoData"]).decode(
        "utf-8")==quote()["memo"]
    assert result["account_reserve_drops"]=="1400000"
    assert result["source_balance_and_reserve_checked"] is True
    assert result["fresh_privy_policy_verified"] is False
    assert result["ready_to_submit"] is False
    assert result["wallet_signed"] is False
    assert result["source_transaction_sent"] is False
    assert result["execution_enabled"] is False
    assert result["can_submit"] is False
    assert len(result["unsigned_payment_sha256"])==64


@pytest.mark.parametrize("mutation",[
    lambda q:q.update(mode="EXECUTABLE"),
    lambda q:q.update(source_wallet=VAULT),
    lambda q:q.update(inbound_vault="not-xrp"),
    lambda q:q.update(destination_wallet=OWNER),
    lambda q:q.update(destination_asset="BTC"),
    lambda q:q.update(amount="100"),
    lambda q:q.update(refund_path_verified=False),
    lambda q:q.update(original_vault_verified=False),
    lambda q:q.update(required_chain_memo_verified=False),
    lambda q:q.update(exact_venue_fees_verified=False),
    lambda q:q.update(transaction_encoding_verified=True),
    lambda q:q.update(source_transaction_sent=True),
    lambda q:q.update(can_submit=True),
    lambda q:q.update(execution_enabled=True),
    lambda q:q.update(memo="=:ETH.USDC:"+ETH+":0/1/0"),
    lambda q:q.update(memo="=:ETH.USDC:"+ETH+"/"+VAULT+":0/1/0"),
    lambda q:q.update(quote_expires_at=datetime.fromtimestamp(NOW-1,tz=timezone.utc)),
    lambda q:q.update(quote_expires_at=datetime.fromtimestamp(NOW+601,tz=timezone.utc)),
])
def test_provider_cannot_redirect_xrp_refund_or_signer(mutation):
    q=quote()
    mutation(q)
    with pytest.raises(ValueError):
        build(quote=q)


@pytest.mark.parametrize("mutation",[
    lambda a:a["result"].update(validated=False),
    lambda a:a["result"].update(ledger_index=0),
    lambda a:a["result"]["account_data"].update(Account=VAULT),
    lambda a:a["result"]["account_data"].update(Sequence=0),
    lambda a:a["result"]["account_data"].update(OwnerCount=-1),
    lambda a:a["result"]["account_data"].update(Balance="NaN"),
    lambda a:a["result"]["account_data"].update(Balance="30000"),
])
def test_chain_account_reserve_and_sequence_are_not_guessed(mutation):
    a=account()
    mutation(a)
    with pytest.raises(ValueError):
        build(account_info=a)


@pytest.mark.parametrize("mutation",[
    lambda n:n.update(validated_ledger_index=10),
    lambda n:n.update(network_fee_drops="0"),
    lambda n:n.update(network_fee_drops="NaN"),
    lambda n:n.update(reserve_base_drops="0"),
    lambda n:n.update(reserve_increment_drops="-1"),
    lambda n:n.update(reserve_base_drops="20000000"),
])
def test_xrp_network_fee_and_live_reserve_required(mutation):
    n=network()
    mutation(n)
    with pytest.raises(ValueError):
        build(network_terms=n)


@pytest.mark.parametrize("amount",[
    "0","-1","0.0000001","NaN","1e5","010","",None,
])
def test_invalid_source_amount_cannot_be_encoded_as_drops(amount):
    q=quote()
    q["amount"]=amount
    with pytest.raises(ValueError):
        build(quote=q,amount=amount)


def test_unsigned_xrp_plan_cannot_use_an_evm_or_bitcoin_wallet():
    a=account()
    a["result"]["account_data"]["Account"]=ETH
    with pytest.raises(ValueError):
        build(account_info=a)
