from fractions import Fraction
import pytest
from backend.staged_net_valuation import ratio,read_ratio,stable_legs,verified_rate

def test_exact_historical_ratio_roundtrip():
    assert read_ratio(ratio(Fraction(17,31)),positive=True)==Fraction(17,31)
    for value in (["1","0"],["-1","2"],[1,"2"],["0","1"]):
        with pytest.raises(ValueError):
            read_ratio(value,positive=True)

def test_sell_values_every_ethereum_credit_not_just_last_hash():
    a="0x"+"1"*64;b="0x"+"2"*64
    order={"side":"SELL","final_received_atomic":300}
    rows=[{"kind":"BRIDGE","state":"FINALIZED","stage":2,"chain_id":56,
           "proof":{"destination_hash":a,"received_atomic":"100"}},
          {"kind":"BRIDGE","state":"FINALIZED","stage":2,"chain_id":56,
           "proof":{"destination_hash":b,"received_atomic":"200"}}]
    assert [x["amount_atomic"] for x in stable_legs(order,rows)]==["100","200"]
    with pytest.raises(ValueError):stable_legs(dict(order,final_received_atomic=301),rows)
    with pytest.raises(ValueError):stable_legs(dict(order,final_received_atomic=200),[rows[0],rows[0]])

def test_buy_uses_original_ethereum_debit_not_transit_credit():
    tx="0x"+"1"*64
    order={"side":"BUY","original_input_atomic":1000}
    steps=[{"kind":"BRIDGE","state":"FINALIZED","stage":1,"chain_id":1,"tx_hash":tx,
            "proof":{"received_atomic":"900"}}]
    assert stable_legs(order,steps)==[{"chain_id":1,"tx_hash":tx,"amount_atomic":"1000"}]

def test_rate_must_match_real_block_and_provider():
    block={"block_unix":1000}
    proof={"asset":"USDC","provider":"COINGECKO_MARKET_CHART_RANGE","execution_block_unix":1000,
           "rate_observed_unix":1100,"usd_price":"0.999"}
    assert verified_rate(proof,asset="USDC",block=block)==Fraction(999,1000)
    for change in ({"execution_block_unix":1001},{"rate_observed_unix":1181},{"asset":"USDT"},{"provider":"SPOT"}):
        with pytest.raises(ValueError):verified_rate({**proof,**change},asset="USDC",block=block)


def test_staged_order_prices_actual_costs_and_all_sell_credits(monkeypatch):
    import asyncio
    from backend import staged_net_valuation as m
    wallet="0x"+"f"*40
    source="0x"+"1"*64;other="0x"+"2"*64
    a="0x"+"3"*64;b="0x"+"4"*64
    steps=[dict(kind="SWAP",state="FINALIZED",stage=1,chain_id=56,tx_hash=source),
           dict(kind="BRIDGE",state="FINALIZED",stage=2,chain_id=56,tx_hash=other,
                proof=dict(destination_hash=a,received_atomic="100")),
           dict(kind="BRIDGE",state="FINALIZED",stage=2,chain_id=56,tx_hash="0x"+"5"*64,
                proof=dict(destination_hash=b,received_atomic="200"))]
    costs=[dict(chain_id=56,tx_hash=s["tx_hash"],native_asset="BNB",gas_atomic="1000000000000000",
                native_bridge_fee_atomic="0") for s in steps]
    original=dict(order=dict(side="SELL",stable="USDC",wallet_address=wallet,final_received_atomic=300),
                  steps=steps,costs=costs)
    blocks={source:1000,other:1001,a:1002,b:1003,steps[-1]["tx_hash"]:1004}
    async def mined(client,chain,tx):
        return dict(chain_id=chain,tx_hash=tx,block_unix=blocks[tx],block_number=1,
            block_hash="0x"+"c"*64,**{"from":wallet},gas_atomic="1000000000000000",
            value_atomic="0",success=True)
    async def historic(*,asset,block_unix,client):
        return dict(asset=asset,provider="COINGECKO_MARKET_CHART_RANGE",execution_block_unix=block_unix,
            rate_observed_unix=block_unix,usd_price=("300" if asset=="BNB" else ("0.99" if block_unix==1002 else "1.02")))
    monkeypatch.setattr(m,"mined_evidence",mined)
    monkeypatch.setattr(m,"fetch_recent_historical_usd_observation",historic)
    evidence=asyncio.run(m.value_staged_order(None,original))
    assert read_ratio(evidence["network_cost_usd"])==Fraction(9,10)
    assert read_ratio(evidence["stable_usd"])==Fraction(101,100)
    assert evidence["stable_atomic"]=="300"
    assert len(evidence["observations"])==5
