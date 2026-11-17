"""Only genuine finalized owner ERC20 Treasury receipt can settle auto HWM."""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend import shared_evm_auto_fee_finality as finality
from backend.shared_evm_fee_transfer_proof import owner_fee_transfer_draft

DID="did:privy:owner12345"
KEY="sig_original_123456789"
TX="0x"+"ee"*32
WALLET="0x"+"ab"*20
TREASURY="0x"+"cd"*20


def sample():
    draft=owner_fee_transfer_draft(
        asset="LINK",stable="USDC",
        wallet=WALLET,treasury=TREASURY,
        fee_due_usd="2.5",stable_usd="1")
    return dict(
        privy_did=DID,asset="LINK",execution_id=KEY,
        stable="USDC",chain_id=1,wallet_address=WALLET,
        treasury_wallet=TREASURY,token_address=draft["token_address"],
        amount_atomic=Decimal(draft["amount_atomic"]),tx_hash=TX,
        payment_asset="LINK",payment_stable="USDC",payment_chain_id=1,
        stable_token_address=draft["token_address"],
        owner_wallet=WALLET,payment_treasury_wallet=TREASURY,
        token_amount_atomic=Decimal(draft["amount_atomic"]),
        fee_due_usd=Decimal("2.5"),stable_usd=Decimal("1"),
        payment_state="PREPARED")


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows);self.sql=[]
    def execute(self,q,args):
        self.sql.append((q,args))
    def fetchone(self):
        return next(self.rows)


def pending(row):
    return dict(
        status="SUBMITTED",tx_hash=row["tx_hash"],
        chain_id=row["chain_id"],stable=row["stable"],
        wallet_address=row["wallet_address"],
        treasury_wallet=row["treasury_wallet"],
        token_address=row["token_address"],
        amount_atomic=row["amount_atomic"])


def test_exact_persisted_owner_fee_recreates_original_privy_transaction():
    source=sample()
    draft=finality.original_auto_fee_draft(source)
    assert draft["owner_wallet"]==WALLET
    assert draft["treasury_wallet"]==TREASURY
    assert draft["amount_atomic"]=="2500000"


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(payment_state="PAID_VERIFIED"),
    lambda r:r.update(payment_stable="USDT"),
    lambda r:r.update(owner_wallet=TREASURY),
    lambda r:r.update(token_amount_atomic=Decimal("2500001")),
    lambda r:r.update(tx_hash=None),
    lambda r:r.update(chain_id=137),
])
def test_foreign_fee_quote_or_unreconciled_payment_cannot_count(mutation):
    source=sample()
    mutation(source)
    with pytest.raises(ValueError):
        finality.original_auto_fee_draft(source)


def test_fee_hwm_payment_and_auto_journal_update_atomically(monkeypatch):
    source=sample()
    calls=[]
    def old_payment(*args,**kw):
        calls.append(("paid",kw))
        return {"fee_paid_onchain":True}
    monkeypatch.setattr(finality,"_paid_locked",old_payment)
    cur=Cur([pending(source),{"status":"PAID_VERIFIED"}])
    r=finality._complete_auto_fee_locked(
        cur,row=source,proof={"onchain_finalized":True})
    assert r["state"]=="PAID_VERIFIED"
    assert len(calls)==1
    assert calls[0][1]["tx_hash"]==TX
    assert "UPDATE mininex_evm_auto_fee_sends" in cur.sql[-1][0]
    assert "AND status='SUBMITTED'" in cur.sql[-1][0]


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(status="RECONCILIATION_REQUIRED"),
    lambda r:r.update(tx_hash="0x"+"aa"*32),
    lambda r:r.update(treasury_wallet=WALLET),
    lambda r:r.update(amount_atomic=Decimal("100")),
])
def test_missing_real_fee_receipt_never_updates_hwm(monkeypatch,mutation):
    source=sample()
    old=pending(source)
    mutation(old)
    monkeypatch.setattr(finality,"_paid_locked",
                        lambda *a,**kw:pytest.fail("must not pay"))
    with pytest.raises(HTTPException):
        finality._complete_auto_fee_locked(
            Cur([old]),row=source,
            proof={"onchain_finalized":True})


def test_fee_finality_alone_does_not_use_wallet_signer():
    import inspect
    src=inspect.getsource(finality)
    assert "post_claimed_privy_evm_rpc" not in src
    assert "sign_privy_rpc_payload" not in src
