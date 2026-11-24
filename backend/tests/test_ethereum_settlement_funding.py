import pytest
from backend.ethereum_settlement_funding import summarize_pending_funding

def test_all_engine_reservations_use_one_stable_wallet_not_one_coin():
    rows=[
        dict(asset="POL",stable="USDC",unit="ATOMIC",amount="10000000"),
        dict(asset="LINK",stable="USDC",unit="USD",amount="20"),
        dict(asset="BTC",stable="USDC",unit="STABLE",amount="15"),
    ]
    report=summarize_pending_funding(rows,asset="POL",stable="USDC",stable_usd="1")
    assert report["pending_wallet_stable_atomic"]=="45000000"
    assert report["pending_coin_usd"]=="10.000000"
    assert report["atomic_claimed"] is False

def test_usd_reservation_rounds_up_and_rejects_wrong_stable():
    rows=[dict(asset="ETH",stable="USDT",unit="USD",amount="1")]
    assert summarize_pending_funding(rows,asset="ETH",stable="USDT",
        stable_usd="0.99")["pending_wallet_stable_atomic"]=="1010102"
    with pytest.raises(ValueError):
        summarize_pending_funding(rows,asset="ETH",stable="USDC",stable_usd="1")


def test_shared_wallet_cannot_reserve_same_funding_twice():
    from backend.ethereum_settlement_funding import assert_ethereum_funds_locked
    from fastapi import HTTPException
    class Cursor:
        def execute(self,sql,args):
            self.sql,self.args=sql,args
        def fetchall(self):
            return [dict(asset="POL",stable="USDC",unit="ATOMIC",amount="7000000")]
    cur=Cursor()
    with pytest.raises(HTTPException):
        assert_ethereum_funds_locked(cur,did="did:privy:owner",asset="LINK",stable="USDC",
            input_atomic=4_000_000,balance_atomic=10_000_000,stable_usd="1")
    assert_ethereum_funds_locked(cur,did="did:privy:owner",asset="BTC",stable="USDC",
        input_atomic=3_000_000,balance_atomic=10_000_000,stable_usd="1",excluded_native_key="native_original")
    assert cur.args["excluded_native_asset"]=="BTC"
    assert cur.args["excluded_native_key"]=="native_original"
    assert "quote_expires_at>now()" in cur.sql
