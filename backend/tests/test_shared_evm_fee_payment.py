"""Five-coin HWM fee collection is one verified obligation and one real receipt."""
from decimal import Decimal
import pytest
from fastapi import HTTPException

from backend import shared_evm_fee_payment as payment
from backend.shared_evm_fee_transfer_proof import owner_fee_transfer_draft
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:account12345"
KEY="sig_abcdef01234567890123456789"
TX="0x"+"cd"*32
WALLET="0x"+"ab"*20
TREASURY="0x"+"ef"*20
ASSETS={"BNB":"USDT","POL":"USDC","AVAX":"USDC",
        "LINK":"USDC","UNI":"USDC"}


def source(asset):
    return {
        "fee_due_usd":Decimal("2.50"),
        "fee_assessed_usd":Decimal("2.50"),
        "source_tx_hash":TX,
        "tx_hash":TX,"status":"FINALIZED_SUCCESS","side":"SELL",
        "stable":ASSETS[asset],
        "chain_id":_pair(asset,ASSETS[asset],"SELL")["chain_id"],
        "wallet_address":WALLET,
    }


def prepared(asset,state="PREPARED",tx_hash=None):
    stable=ASSETS[asset]
    draft=owner_fee_transfer_draft(
        asset=asset,stable=stable,wallet=WALLET,treasury=TREASURY,
        fee_due_usd="2.50",stable_usd="1")
    return dict(
        asset=asset,stable=stable,chain_id=draft["chain_id"],
        stable_token_address=draft["token_address"],
        owner_wallet=WALLET,treasury_wallet=TREASURY,
        fee_due_usd=Decimal("2.50"),stable_usd=Decimal("1"),
        token_amount_atomic=Decimal(draft["amount_atomic"]),
        state=state,tx_hash=tx_hash,
    )


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows);self.sql=[]
    def execute(self,q,args):
        self.sql.append((q,args))
    def fetchone(self):
        return next(self.rows)


@pytest.mark.parametrize("asset",list(ASSETS))
def test_one_payment_intent_from_original_hwm_sell(asset):
    cur=Cursor([source(asset),None])
    result=payment._prepare_locked(
        cur,did=DID,asset=asset,key=KEY,wallet=WALLET,
        treasury=TREASURY,rate=Decimal("1"))
    assert result["execution_id"]==KEY
    assert result["state"]=="PREPARED"
    assert result["requires_owner_privy_signature"] is True
    assert result["paid_onchain"] is False
    assert len(cur.sql)==3
    assert "mininex_evm_fee_obligations" in cur.sql[0][0]
    assert "FOR UPDATE OF o,e,b" in cur.sql[0][0]
    assert "INSERT INTO mininex_evm_verified_fee_payments" in cur.sql[-1][0]
    assert cur.sql[-1][1][2]==KEY


@pytest.mark.parametrize("asset",list(ASSETS))
def test_repeat_prepare_returns_immutable_existing_transfer_not_new_quote(asset):
    cur=Cursor([source(asset),prepared(asset)])
    result=payment._prepare_locked(
        cur,did=DID,asset=asset,key=KEY,wallet=WALLET,
        treasury=TREASURY,rate=Decimal("1.05"))
    assert result["stablecoin_usd_rate"]=="1"
    assert result["execution_id"]==KEY
    assert len(cur.sql)==2
    assert not any("INSERT" in q for q,args in cur.sql)


@pytest.mark.parametrize("change",[
    lambda s:s.update(side="BUY"),
    lambda s:s.update(status="SUBMITTED"),
    lambda s:s.update(wallet_address="0x"+"cd"*20),
    lambda s:s.update(tx_hash="0x"+"ef"*32),
    lambda s:s.update(fee_due_usd=Decimal("100")),
    lambda s:s.update(fee_assessed_usd=Decimal("0")),
])
def test_no_fake_or_unapproved_hwm_obligation_can_be_paid(change):
    wrong=source("LINK")
    change(wrong)
    cur=Cursor([wrong])
    with pytest.raises(HTTPException):
        payment._prepare_locked(cur,did=DID,asset="LINK",key=KEY,
                                wallet=WALLET,treasury=TREASURY,
                                rate=Decimal("1"))
    assert len(cur.sql)==1


def proof(asset):
    row=prepared(asset)
    return {
        "mode":"FINALIZED_OWNER_SIGNED_EVM_FEE_PAYMENT",
        "asset":asset,"chain_id":row["chain_id"],
        "wallet_address":WALLET,"treasury_wallet":TREASURY,
        "token_address":row["stable_token_address"],
        "amount_atomic":str(row["token_amount_atomic"].to_integral_value()),
        "tx_hash":TX,"onchain_finalized":True,
    }


@pytest.mark.parametrize("asset",list(ASSETS))
def test_only_finalized_matching_onchain_transfer_can_credit_fee(asset):
    cur=Cursor([source(asset),prepared(asset),{"state":"PAID_VERIFIED"}])
    result=payment._paid_locked(
        cur,did=DID,asset=asset,key=KEY,wallet=WALLET,
        tx_hash=TX,proof=proof(asset))
    assert result["state"]=="PAID_VERIFIED"
    assert result["fee_paid_onchain"] is True
    assert "AND state='PREPARED'" in cur.sql[-1][0]
    assert len(cur.sql)==3


@pytest.mark.parametrize("asset",list(ASSETS))
def test_repeated_same_blockchain_hash_does_not_debit_again(asset):
    cur=Cursor([source(asset),prepared(asset,"PAID_VERIFIED",TX)])
    result=payment._paid_locked(
        cur,did=DID,asset=asset,key=KEY,wallet=WALLET,
        tx_hash=TX,proof=proof(asset))
    assert result["duplicate"] is True
    assert result["state"]=="PAID_VERIFIED"
    assert len(cur.sql)==2


@pytest.mark.parametrize("asset",list(ASSETS))
def test_another_hash_cannot_pay_original_obligation_twice(asset):
    cur=Cursor([source(asset),prepared(asset,"PAID_VERIFIED","0x"+"ab"*32)])
    with pytest.raises(HTTPException):
        payment._paid_locked(
            cur,did=DID,asset=asset,key=KEY,wallet=WALLET,
            tx_hash=TX,proof=proof(asset))
    assert len(cur.sql)==2


@pytest.mark.parametrize("tamper",[
    lambda p:p.update(wallet_address="0x"+"aa"*20),
    lambda p:p.update(treasury_wallet="0x"+"aa"*20),
    lambda p:p.update(amount_atomic="10"),
    lambda p:p.update(onchain_finalized=False),
    lambda p:p.update(tx_hash="0x"+"aa"*32),
])
def test_no_unverified_fee_receipt_can_reach_paid_state(tamper):
    p=proof("LINK")
    tamper(p)
    cur=Cursor([source("LINK"),prepared("LINK")])
    with pytest.raises(HTTPException):
        payment._paid_locked(
            cur,did=DID,asset="LINK",key=KEY,wallet=WALLET,
            tx_hash=TX,proof=p)
    assert not any(q.lstrip().startswith("UPDATE ") for q,args in cur.sql)


def test_fee_routes_are_user_authenticated_and_never_auto_sign():
    from backend.app import app
    routes={route.path:route for route in app.routes}
    root="/api/live/evm/fee-payment/{asset}/{execution_id}"
    assert root in routes
    assert root+"/prepare" in routes
    assert root+"/confirm" in routes
    from backend.shared_evm_fee_payment import prepare_owner_verified_evm_fee
    import inspect
    code=inspect.getsource(prepare_owner_verified_evm_fee)
    assert "post_claimed_privy_evm_rpc" not in code
    assert "sign_privy_rpc_payload" not in code
