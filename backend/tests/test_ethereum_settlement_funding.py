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


def test_existing_across_buy_reservation_is_rechecked_not_double_counted():
    from backend.ethereum_settlement_funding import assert_reserved_ethereum_funds_locked
    from fastapi import HTTPException
    class Cursor:
        def execute(self,sql,args): self.sql=sql
        def fetchall(self):
            return [dict(asset="POL",stable="USDC",unit="ATOMIC",amount="7000000"),
                    dict(asset="LINK",stable="USDC",unit="ATOMIC",amount="3000000")]
    cur=Cursor()
    kw=dict(did="did:privy:owner",asset="POL",stable="USDC",input_atomic=7_000_000,stable_usd="1")
    assert assert_reserved_ethereum_funds_locked(cur,**kw,balance_atomic=10_000_000)["pending_wallet_stable_atomic"]=="10000000"
    assert "mininex_across_staged_orders" in cur.sql
    with pytest.raises(HTTPException):
        assert_reserved_ethereum_funds_locked(cur,**kw,balance_atomic=9_999_999)


@pytest.mark.parametrize("asset",["BTC","XRP","SOL","TRX"])
def test_wrapped_spot_profit_does_not_block_shared_funding(asset):
    def fill(key,side,source,dest,family="WRAPPED_SPOT"):
        return dict(asset=asset,stable="USDC",unit="FINALIZED_PROFIT_INPUT",
            family=family,coin_wallet="same-owner",fill_key=key,side=side,
            source_amount=source,destination_amount=dest,automatic=True)
    rows=[fill("native-buy","BUY","1000","1","NATIVE"),
          fill("wrapped-buy","BUY","100","1"),
          fill("wrapped-sell","SELL","1","150"),
          fill("native-sell","SELL","1","1200","NATIVE")]
    report=summarize_pending_funding(rows,asset="ETH",stable="USDC",stable_usd="1")
    assert report["protected_profit_gross_atomic"]=="250000000"
    assert report["pending_wallet_stable_atomic"]=="250000000"
    assert report["unmatched_profit_sells"]==0
    # The same wallet text does not make native units available for a token SELL.
    report=summarize_pending_funding([rows[0],rows[2]],asset="ETH",stable="USDC",stable_usd="1")
    assert report["protected_profit_gross_atomic"]=="0"
    assert report["unmatched_profit_sells"]==1


def test_profit_sql_uses_wrapped_spot_family_and_unique_cross_parent():
    from backend.realized_profit_reserve import PROFIT_FILLS_SQL
    assert "THEN 'WRAPPED_SPOT'" in PROFIT_FILLS_SQL
    assert "'CROSS_EVM',q.quote_id::text" in PROFIT_FILLS_SQL


def test_verified_net_reserve_includes_fee_without_double_counting():
    def fill(key,side,source,dest,stable="USDC",cost="2"):
        amount=int(source if side=="BUY" else dest)*10**6
        return dict(asset="LINK",stable=stable,unit="FINALIZED_PROFIT_INPUT",
            family="ETHEREUM",coin_wallet="original",fill_key=key,side=side,
            source_amount=source,destination_amount=dest,automatic=True,
            evidence={"version":1,"side":side,"stable":stable,"stable_atomic":str(amount),
                      "stable_usd":["1","1"],"network_cost_usd":[cost,"1"]})
    rows=[fill("a","BUY","100","1","USDT"),fill("b","SELL","1","120","USDC")]
    r=summarize_pending_funding(rows,asset="LINK",stable="USDC",stable_usd="1")
    assert r["protected_profit_net_atomic"]=="16000000"
    assert r["protected_fee_due_atomic"]=="800000"
    assert r["protected_saved_net_atomic"]=="15200000"
    assert r["pending_wallet_stable_atomic"]=="16000000"
    assert r["profit_reserve_net_verified"]
    assert summarize_pending_funding(rows,asset="LINK",stable="USDT",stable_usd="1")["pending_wallet_stable_atomic"]=="0"
    rows[0]["evidence"]=None
    r=summarize_pending_funding(rows,asset="LINK",stable="USDC",stable_usd="1")
    assert r["protected_profit_provisional_atomic"]=="120000000"
    assert r["protected_fee_due_atomic"]=="0"
    assert not r["profit_reserve_net_verified"]
