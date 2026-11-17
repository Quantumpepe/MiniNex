"""The exact owner-approved Privy atomic ceiling covers each real EVM swap."""
from datetime import datetime,timezone

import pytest
from fastapi import HTTPException
from backend.shared_evm_policy_swap_gate import check_evm_policy_bound_swap
from backend.shared_evm_privy_user_policy import build_user_evm_policy_blueprint
from backend.shared_evm_execution import EvmPair,unsigned_exact_input_single
from backend.shared_evm_swap_receipts import _pair
import hashlib,json

WALLET="0x"+"ab"*20
NOW=1800000000


def scenario(asset,side="BUY",amount=1000000):
    stable="USDT" if asset=="BNB" else "USDC"
    candidate=build_user_evm_policy_blueprint(
        asset=asset,stable=stable,wallet=WALLET,
        owner_id="owner_123456",executor_id="signer_123456",
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=(
            "1000000000000000000" if asset in ("BNB","POL","AVAX") else None),
        expires_at=NOW+3600,now=NOW)
    digest=hashlib.sha256(json.dumps(
        candidate,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    pair=_pair(asset,stable,side)
    record={
        "asset":asset,"stable":stable,"wallet_address":WALLET,
        "owner_id":"owner_123456","signer_id":"signer_123456",
        "candidate_json":candidate,"policy_sha256":digest,
        "expires_at":datetime.fromtimestamp(NOW+3600,timezone.utc),
    }
    tx=unsigned_exact_input_single(
        pair=EvmPair(
            asset,pair["chain_id"],pair["network"],
            pair["source_token"],pair["output_token"],pair["router"],500),
        wallet=WALLET,amount_in=amount,min_out=1)
    return record,tx,stable


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
@pytest.mark.parametrize("side,amount",[
    ("BUY",1000000),("SELL",1000000000000000000)])
def test_five_coin_exact_policy_cap_accepts_user_approved_max(asset,side,amount):
    record,tx,stable=scenario(asset,side,amount)
    result=check_evm_policy_bound_swap(
        record=record,asset=asset,side=side,stable=stable,
        wallet=WALLET,tx=tx,input_atomic=str(amount),
        minimum_output_atomic="1",now=NOW)
    assert result["atomic_input_within_owner_policy"]
    assert result["wallet_signing_permitted_by_this_check"] is False
    assert result["transaction_submitted"] is False


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_missing_extra_approval_does_not_inherit_other_network_cap(asset):
    record,tx,stable=scenario(asset,amount=1000001)
    with pytest.raises(HTTPException) as err:
        check_evm_policy_bound_swap(
            record=record,asset=asset,side="BUY",stable=stable,
            wallet=WALLET,tx=tx,input_atomic="1000001",
            minimum_output_atomic="1",now=NOW)
    assert err.value.status_code==409


@pytest.mark.parametrize("tamper",[
    lambda r,t:r.update(wallet_address="0x"+"cd"*20),
    lambda r,t:r.update(policy_sha256="b"*64),
    lambda r,t:r.update(stable="USDT"),
    lambda r,t:t.update(chainId=1),
    lambda r,t:t.update(**{"from":"0x"+"cd"*20}),
    lambda r,t:t.update(to="0x"+"cd"*20),
])
def test_changed_wallet_quote_chain_or_digest_does_not_pass(tamper):
    record,tx,stable=scenario("AVAX")
    tamper(record,tx)
    with pytest.raises(HTTPException):
        check_evm_policy_bound_swap(
            record=record,asset="AVAX",side="BUY",stable=stable,
            wallet=WALLET,tx=tx,input_atomic="1000000",
            minimum_output_atomic="1",now=NOW)
