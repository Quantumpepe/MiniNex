"""Actual user-approved EVM treasury Transfer proof, no simulated payment."""
import copy
from decimal import Decimal
import pytest
from fastapi import HTTPException

from backend.shared_evm_fee_transfer_proof import (
    exact_fee_token_units,owner_fee_transfer_draft,
    verified_owner_fee_transfer, owner_fee_treasury,
)
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
TREASURY="0x"+"cd"*20
TX="0x"+"12"*32
BLOCK="0x"+"34"*32
TOKENS={"BNB":"USDT","POL":"USDC","AVAX":"USDC",
        "LINK":"USDC","UNI":"USDC","ARB":"USDC"}
TOPIC="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def evidence(asset):
    stable=TOKENS[asset]
    p=owner_fee_transfer_draft(
        asset=asset,stable=stable,wallet=WALLET,treasury=TREASURY,
        fee_due_usd="1.250000",stable_usd="0.999000")
    tx={"hash":TX,"from":WALLET,"to":p["token_address"],"value":"0x0",
        "input":p["unsigned_transaction"]["data"],"blockHash":BLOCK}
    log={"address":p["token_address"],
         "topics":[TOPIC,"0x"+f"{int(WALLET,16):064x}",
                   "0x"+f"{int(TREASURY,16):064x}"],
         "data":"0x"+f"{int(p['amount_atomic']):064x}"}
    receipt={"transactionHash":TX,"status":"0x1","blockHash":BLOCK,
             "blockNumber":"0xc","logs":[log]}
    block={"number":"0xc","hash":BLOCK}
    finalized={"number":"0xe"}
    return p,tx,receipt,block,finalized


@pytest.mark.parametrize("asset",list(TOKENS))
def test_fee_transfer_exact_onchain_erc20_event_owner_treasury(asset):
    draft,tx,receipt,block,finalized=evidence(asset)
    route=_pair(asset,TOKENS[asset],"BUY")
    assert draft["chain_id"]==route["chain_id"]
    assert draft["token_address"]==route["source_token"].lower()
    assert len(draft["unsigned_transaction"]["data"])==138
    assert draft["unsigned_transaction"]["data"].startswith("0xa9059cbb")
    assert draft["requires_owner_privy_signature"] is True
    assert draft["backend_can_autosend"] is False
    result=verified_owner_fee_transfer(
        draft=draft,tx_hash=TX,transaction=tx,receipt=receipt,
        finalized_block=finalized,mined_block=block)
    assert result["onchain_finalized"] is True
    assert result["wallet_address"]==WALLET
    assert result["amount_atomic"]==draft["amount_atomic"]


@pytest.mark.parametrize("mutate",[
    lambda p,t,r,b,f:t.update({"from":"evil"}),
    lambda p,t,r,b,f:t.update(to=TREASURY),
    lambda p,t,r,b,f:t.update(input="0x"+"00"*68),
    lambda p,t,r,b,f:r.update(status="0x0"),
    lambda p,t,r,b,f:r.update(blockHash="0x"+"ef"*32),
    lambda p,t,r,b,f:f.update(number="0xb"),
    lambda p,t,r,b,f:r["logs"][0].update(address=TREASURY),
    lambda p,t,r,b,f:r["logs"][0].update(data="0x"+f"{1:064x}"),
    lambda p,t,r,b,f:r["logs"][0]["topics"].__setitem__(2,"0x"+"00"*32),
])
def test_tampered_or_unfinalized_fee_never_marked_paid(mutate):
    draft,tx,receipt,block,finalized=evidence("LINK")
    mutate(draft,tx,receipt,block,finalized)
    with pytest.raises(ValueError):
        verified_owner_fee_transfer(
            draft=draft,tx_hash=TX,transaction=tx,receipt=receipt,
            finalized_block=finalized,mined_block=block)


@pytest.mark.parametrize("asset",list(TOKENS))
def test_no_fee_calculation_assumes_stable_parity(asset):
    decimals=_pair(asset,TOKENS[asset],"BUY")["source_decimals"]
    amount=exact_fee_token_units(
        fee_due_usd="1.25",stable_usd="0.999",decimals=decimals)
    assert amount>int(Decimal("1.25")*Decimal(10)**decimals)


@pytest.mark.parametrize("fee,price",[
    ("0","1"),("-1","1"),("NaN","1"),("1","NaN"),
    ("1","0.89"),("1","1.06"),("1","0"),("abc","1"),
])
def test_fee_never_quoted_from_missing_hwm_or_depegged_rate(fee,price):
    with pytest.raises(ValueError):
        exact_fee_token_units(fee_due_usd=fee,stable_usd=price,decimals=6)


def test_duplicate_matching_fee_transfer_logs_are_rejected():
    draft,tx,receipt,block,finalized=evidence("LINK")
    receipt["logs"].append(copy.deepcopy(receipt["logs"][0]))
    with pytest.raises(ValueError):
        verified_owner_fee_transfer(
            draft=draft,tx_hash=TX,transaction=tx,receipt=receipt,
            finalized_block=finalized,mined_block=block)


@pytest.mark.parametrize("stable,contract",[
    ("USDC","0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    ("USDT","0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
])
def test_arb_fee_draft_uses_42161_stable_contract_not_eth_mainnet(stable,contract):
    draft=owner_fee_transfer_draft(
        asset="ARB",stable=stable,wallet=WALLET,treasury=TREASURY,
        fee_due_usd="1.25",stable_usd="0.999")
    assert draft["chain_id"]==42161
    assert draft["network"]=="arbitrum-one-mainnet"
    assert draft["token_address"]==contract.lower()
    assert draft["decimals"]==6
    assert draft["unsigned_transaction"]["chainId"]==42161
    assert draft["unsigned_transaction"]["to"].lower()==contract.lower()
    assert draft["requires_owner_privy_signature"] is True
    assert draft["wallet_signed"] is False
    assert draft["backend_can_autosend"] is False


def test_arb_treasury_requires_separate_arbitrum_env_without_falling_back(monkeypatch):
    monkeypatch.setenv("MININEX_TREASURY_WALLET",WALLET)
    monkeypatch.delenv("MININEX_TREASURY_ARB_WALLET",raising=False)
    with pytest.raises(HTTPException) as err:
        owner_fee_treasury(asset="ARB",stable="USDC")
    assert err.value.status_code==503
    monkeypatch.setenv("MININEX_TREASURY_ARB_WALLET",TREASURY)
    assert owner_fee_treasury(asset="ARB",stable="USDC")==TREASURY
    assert owner_fee_treasury(asset="ARB",stable="USDT")==TREASURY
    monkeypatch.setenv("MININEX_TREASURY_ARB_WALLET","not_an_address")
    with pytest.raises(HTTPException) as err:
        owner_fee_treasury(asset="ARB",stable="USDC")
    assert err.value.status_code==503


def test_arb_wrong_chain_or_wrong_token_payment_never_claims_success():
    draft,tx,receipt,block,finalized=evidence("ARB")
    assert verified_owner_fee_transfer(
        draft=draft,tx_hash=TX,transaction=tx,receipt=receipt,
        finalized_block=finalized,mined_block=block)["onchain_finalized"]
    wrong=copy.deepcopy(tx)
    wrong["to"]="0x"+"99"*20
    with pytest.raises(ValueError):
        verified_owner_fee_transfer(
            draft=draft,tx_hash=TX,transaction=wrong,receipt=receipt,
            finalized_block=finalized,mined_block=block)
    wrong_receipt=copy.deepcopy(receipt)
    wrong_receipt["logs"][0]["address"]="0x"+"88"*20
    with pytest.raises(ValueError):
        verified_owner_fee_transfer(
            draft=draft,tx_hash=TX,transaction=tx,receipt=wrong_receipt,
            finalized_block=finalized,mined_block=block)
