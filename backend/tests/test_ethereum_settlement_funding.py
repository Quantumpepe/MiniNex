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


def test_protected_cash_never_becomes_another_coin_buy_or_own_exposure():
    def fill(key,side,source,dest,asset="POL",family="CROSS_EVM",automatic=True):
        return dict(asset=asset,stable="USDC",unit="FINALIZED_PROFIT_INPUT",
            family=family,coin_wallet="original-wallet",fill_key=key,side=side,
            source_amount=source,destination_amount=dest,automatic=automatic)
    rows=[fill("buy","BUY","1000","10"),fill("sell","SELL","10","1300"),
          fill("buy2","BUY","1000","10"),fill("sell2","SELL","10","900"),
          dict(asset="LINK",stable="USDC",unit="ATOMIC",amount="20000000")]
    report=summarize_pending_funding(rows,asset="POL",stable="USDC",stable_usd="1")
    assert report["protected_profit_gross_atomic"]=="300000000"
    assert report["pending_wallet_stable_atomic"]=="320000000"
    assert report["pending_coin_usd"]=="0"
    assert report["pending_entries"]==1
    assert not report["profit_reserve_net_verified"]


def test_partial_sale_fifo_and_native_settlement_profit():
    def row(key,side,source,dest,auto=True):
        return dict(asset="BTC",stable="USDT",unit="FINALIZED_PROFIT_INPUT",
            family="NATIVE",coin_wallet="btc-owner",fill_key=key,side=side,
            source_amount=source,destination_amount=dest,automatic=auto)
    rows=[row("a","BUY","1000","0.01"),row("b","SELL","0.004","520"),
          row("c","SELL","0.002","260",False),row("d","SELL","0.004","500")]
    r=summarize_pending_funding(rows,asset="ETH",stable="USDT",stable_usd="1")
    assert r["protected_profit_gross_atomic"]=="220000000"
    assert r["pending_coin_usd"]=="0"


def test_unmatched_sell_is_not_fabricated_profit():
    row=dict(asset="ETH",stable="USDC",unit="FINALIZED_PROFIT_INPUT",family="ETHEREUM",
        coin_wallet="owner",fill_key="external",side="SELL",source_amount="1",
        destination_amount="3000",automatic=True)
    r=summarize_pending_funding([row],asset="ETH",stable="USDC",stable_usd="1")
    assert r["protected_profit_gross_atomic"]=="0"
    assert r["unmatched_profit_sells"]==1


def test_all_twelve_coins_share_the_same_protected_cash_rule():
    from backend.ethereum_settlement_plan import COIN_NETWORKS
    rows=[]
    for asset,(_,_,_,family) in COIN_NETWORKS.items():
        common=dict(asset=asset,stable="USDC",unit="FINALIZED_PROFIT_INPUT",
            family=family,coin_wallet="original",automatic=True)
        rows.extend([{**common,"fill_key":asset+"-buy","side":"BUY","source_amount":"100","destination_amount":"1"},
                     {**common,"fill_key":asset+"-sell","side":"SELL","source_amount":"1","destination_amount":"110"}])
    r=summarize_pending_funding(rows,asset="POL",stable="USDC",stable_usd="1")
    assert r["protected_profit_gross_atomic"]=="120000000"
    assert len(r["protected_profit_by_asset_atomic"])==12
