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
