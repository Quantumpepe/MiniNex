"""Original Ethereum funding transaction encoder for BTC/SOL/XRP BUY.

No Privy calls, approvals or broadcasts; ABI must round-trip exactly.
"""
from datetime import datetime,timezone

import pytest
from eth_abi import decode

from backend import native_eth_thor_deposit_tx as tx
from backend import native_live_eth_buy_source as reader
from backend.live_eth_quotes import STABLES
from backend.manual_swap_live_quotes import THOR_ASSETS

NOW=1803000000
ETH="0x"+"a"*40
VAULT="0x"+"b"*40
ROUTER="0x"+"c"*40
NATIVE={"BTC":"bc1q"+"q"*38,"SOL":"7"*44,"XRP":"r"+"a"*33}


def quote(asset="BTC",stable="USDC",amount="50"):
    return {
        "mode":"THORCHAIN_DESTINATION_BOUND_READ_ONLY",
        "source_asset":stable,"destination_asset":asset,
        "amount":amount,
        "from_network":"ethereum-mainnet",
        "to_network":{
            "BTC":"bitcoin-mainnet","SOL":"solana-mainnet",
            "XRP":"xrpl-mainnet"}[asset],
        "source_wallet":ETH,"destination_wallet":NATIVE[asset],
        "inbound_vault":VAULT,"inbound_router":ROUTER,
        "memo":"=:"+THOR_ASSETS[asset][0]+":"+NATIVE[asset]+"/"+ETH+":0/1/0",
        "quote_expires_at":datetime.fromtimestamp(NOW+120,tz=timezone.utc),
        "original_vault_verified":True,
        "refund_path_verified":True,
        "required_chain_memo_verified":True,
        "exact_venue_fees_verified":True,
        "needs_erc20_router":True,
        "transaction_encoding_verified":False,
        "source_transaction_sent":False,
        "execution_enabled":False,
        "can_submit":False,
    }


@pytest.mark.parametrize("asset,stable,amount",[
    ("BTC","USDC","50"),
    ("SOL","USDT","0.000001"),
    ("XRP","USDC","500.123456"),
])
def test_exact_original_eth_native_buy_abi_roundtrip(asset,stable,amount):
    original=quote(asset,stable,amount)
    report=tx.encode_original_eth_native_buy_deposit(
        original,asset=asset,stable=stable,amount=amount,now=NOW)
    calldata=report["unsigned_transaction"]["data"]
    assert calldata.startswith("0x"+tx._SELECTOR.hex())
    decoded=decode(["address","address","uint256","string","uint256"],
                   bytes.fromhex(calldata[10:]))
    assert decoded[0].lower()==VAULT.lower()
    assert decoded[1].lower()==STABLES[stable].lower()
    assert decoded[2]==int(report["source_amount_atomic"])
    assert decoded[3]==original["memo"]
    assert decoded[4]>=NOW+60*60
    assert report["unsigned_transaction"]["from"]==ETH
    assert report["unsigned_transaction"]["to"]==ROUTER
    assert report["unsigned_transaction"]["value"]=="0x0"
    assert report["unsigned_transaction"]["chainId"]==1
    parsed=reader._read_deposit_call(calldata)
    assert parsed["memo"]==original["memo"]
    assert parsed["atomic"]==decoded[2]
    assert parsed["expiry"]==decoded[4]
    assert report["ready_to_sign"] is False
    assert report["token_allowance_or_permit_verified"] is False
    assert report["remote_privy_owner_grant_verified"] is False
    assert report["wallet_signed"] is False
    assert report["source_transaction_sent"] is False
    assert report["can_submit"] is False


@pytest.mark.parametrize("mutation",[
    {"mode":"FAKE_EXECUTABLE"},
    {"source_asset":"BTC"},
    {"destination_asset":"DOGE"},
    {"amount":"49"},
    {"from_network":"bitcoin-mainnet"},
    {"to_network":"ethereum-mainnet"},
    {"source_wallet":NATIVE["BTC"]},
    {"inbound_vault":"fake"},
    {"inbound_router":"fake"},
    {"memo":"=:BTC.BTC:"+NATIVE["BTC"]+":0/1/0"},
    {"memo":"=:BTC.BTC:"+ETH+"/"+ETH+":0/1/0"},
    {"memo":"=:BTC.BTC:"+NATIVE["BTC"]+"/0x"+"f"*40+":0/1/0"},
    {"original_vault_verified":False},
    {"refund_path_verified":False},
    {"required_chain_memo_verified":False},
    {"exact_venue_fees_verified":False},
    {"needs_erc20_router":False},
    {"transaction_encoding_verified":True},
    {"source_transaction_sent":True},
    {"execution_enabled":True},
    {"can_submit":True},
    {"quote_expires_at":datetime.fromtimestamp(NOW-1,tz=timezone.utc)},
    {"quote_expires_at":datetime.fromtimestamp(NOW+1200,tz=timezone.utc)},
])
def test_forged_untrusted_or_expired_quote_never_creates_unsigned_deposit(mutation):
    case=quote()
    case.update(mutation)
    with pytest.raises(ValueError):
        tx.encode_original_eth_native_buy_deposit(
            case,asset="BTC",stable="USDC",amount="50",now=NOW)


@pytest.mark.parametrize("asset,stable,amount",[
    ("XRP","USDT","50"),
    ("BTC","USDC","50.0000001"),
    ("SOL","USDC","NaN"),
    ("BTC","USDC","-1"),
    ("BTC","USDC","0"),
])
def test_unpayable_or_wrong_network_input_never_rounds_amount(asset,stable,amount):
    case=quote(asset,stable,amount)
    with pytest.raises(ValueError):
        tx.encode_original_eth_native_buy_deposit(
            case,asset=asset,stable=stable,amount=amount,now=NOW)


def test_original_unsigned_prep_has_no_privy_send_or_auth_side_effects():
    import inspect
    code=inspect.getsource(tx.encode_original_eth_native_buy_deposit)
    for action in ("send_transaction","sign_transaction","httpx.",
                   "getAccessToken","psycopg","PrivyClient"):
        assert action not in code
