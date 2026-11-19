"""Only genuine finalized owner ERC20 Treasury receipt can settle auto HWM."""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend import shared_evm_auto_fee_finality as finality
from backend.shared_evm_fee_transfer_proof import owner_fee_transfer_draft
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:owner12345"
KEY="sig_original_123456789"
TX="0x"+"ee"*32
WALLET="0x"+"ab"*20
TREASURY="0x"+"cd"*20


def sample(asset="LINK"):
    route=_pair(asset,"USDC","BUY")
    draft=owner_fee_transfer_draft(
        asset=asset,stable="USDC",
        wallet=WALLET,treasury=TREASURY,
        fee_due_usd="2.5",stable_usd="1")
    return dict(
        privy_did=DID,asset=asset,execution_id=KEY,
        stable="USDC",chain_id=route["chain_id"],wallet_address=WALLET,
        treasury_wallet=TREASURY,token_address=draft["token_address"],
        amount_atomic=Decimal(draft["amount_atomic"]),tx_hash=TX,
        payment_asset=asset,payment_stable="USDC",payment_chain_id=route["chain_id"],
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


@pytest.mark.parametrize("asset", ["LINK", "ARB"])
def test_exact_persisted_owner_fee_recreates_original_privy_transaction(asset):
    source=sample(asset)
    assert source["chain_id"]==_pair(asset,"USDC","BUY")["chain_id"]
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


@pytest.mark.parametrize("asset", ["LINK", "ARB"])
def test_fee_hwm_payment_and_auto_journal_update_atomically(monkeypatch,asset):
    source=sample(asset)
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


def test_stale_pending_treasury_receipts_cannot_hide_new_paid_arb_fees(monkeypatch):
    """The one-shot receipt verifier traverses every work page without resending."""
    records=[dict(sample("ARB"),execution_id=f"sig_original_{i:08d}")
             for i in range(5)]
    seen=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,query,args):
            assert "a.status='SUBMITTED'" in query
            assert "a.tx_hash IS NOT NULL" in query
            assert "LIMIT %s OFFSET %s" in query
            self.limit,self.offset=args
            seen.append(args)
        def fetchall(self):return records[self.offset:self.offset+self.limit]
    monkeypatch.setattr(finality,"_AUTO_FEE_FINALITY_OFFSET",0)
    monkeypatch.setattr(finality,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(finality,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:DB())
    pages=[finality._submitted_automatic_owner_fee_records(limit=2)
           for _ in range(4)]
    assert pages==[records[:2],records[2:4],records[4:],records[:2]]
    assert seen==[(2,0),(2,2),(2,4),(2,5),(2,0)]
