from fractions import Fraction
import pytest
from backend.realized_net_accounting import calculate_realized_net,historical_cost_usd
from backend.staged_fee_evidence import staged_cost_evidence
from backend.ethereum_settlement_plan import COIN_NETWORKS

def fill(key,side,source,destination,asset="ETH",**kw):
    return dict(asset=asset,family=COIN_NETWORKS[asset][3],coin_wallet="owner",
        fill_key=key,side=side,source_amount=source,destination_amount=destination,
        automatic=True,stable="USDC",stable_usd="1",network_cost_usd="2") | kw

@pytest.mark.parametrize("asset",list(COIN_NETWORKS))
def test_positive_net_fee_on_every_coin_without_hwm(asset):
    rows=[fill("a","BUY","100","1",asset),
          fill("b","SELL","1","90",asset),
          fill("c","BUY","100","1",asset),
          fill("d","SELL","1","120",asset)]
    loss,gain=calculate_realized_net(rows)
    assert loss["realized_net_usd"]=="-14.000000000000"
    assert loss["fee_due_stable_atomic"]=="0"
    assert gain["realized_net_usd"]=="16.000000000000"
    assert gain["fee_due_stable_atomic"]=="800000"
    assert not gain["payment_authorized"]

def test_partial_fifo_with_cross_stable_and_historical_rates():
    rows=[fill("a","BUY","100","2",stable_usd="0.99",network_cost_usd="1"),
          fill("b","SELL","1","70",stable="USDT",stable_usd="1",network_cost_usd="2"),
          fill("c","SELL","1","60",stable="USDT",stable_usd="1.01",network_cost_usd="1")]
    a,b=calculate_realized_net(rows)
    assert a["realized_net_usd"]=="18.000000000000"
    assert a["fee_due_stable_atomic"]=="900000"
    assert b["realized_net_usd"]=="9.600000000000"
    assert b["fee_due_stable_atomic"]=="475247"

def test_missing_cost_and_unmatched_inventory_never_charge():
    rows=[fill("a","BUY","100","1",network_cost_usd=None),
          fill("b","SELL","1","120"),
          fill("c","SELL","1","120")]
    a,b=calculate_realized_net(rows)
    assert a["reason"]=="HISTORICAL_COST_EVIDENCE_REQUIRED"
    assert b["reason"]=="UNMATCHED_INVENTORY"
    assert a["fee_due_stable_atomic"] is b["fee_due_stable_atomic"] is None

def test_manual_sale_consumes_known_lot_but_no_fee():
    rows=[fill("a","BUY","100","2"),
          fill("b","SELL","1","80",automatic=False),
          fill("c","SELL","1","80")]
    result=calculate_realized_net(rows)
    assert len(result)==1 and result[0]["realized_net_usd"]=="27.000000000000"

def test_wrapped_and_native_positions_not_mixed():
    rows=[fill("a","BUY","100","1","BTC"),
          fill("b","SELL","1","150","BTC",family="WRAPPED_SPOT")]
    assert calculate_realized_net(rows)[0]["unmatched_inventory"]
    with pytest.raises(ValueError):
        calculate_realized_net([rows[0],rows[0]])

def test_staged_cost_evidence_keeps_chain_gas_and_separate_bridge_fee():
    a="0x"+"a"*64;b="0x"+"b"*64
    common=dict(order_id="original",privy_did="owner",stage=1,state="FINALIZED")
    rows=[dict(common,chain_id=1,tx_hash=a,kind="BRIDGE",prepared={"native_bridge_fee_atomic":"1000000000000000"},
          proof={"source_hash":a,"state":"BRIDGE_CREDIT_VERIFIED","source_gas_atomic":"2000000000000000","native_bridge_fee_atomic":"1000000000000000"}),
          dict(common,chain_id=43114,tx_hash=b,stage=2,kind="SWAP",prepared={},
          proof={"source_hash":b,"state":"SWAP_CREDIT_VERIFIED","source_gas_atomic":"100000000000000000"})]
    kw=dict(asset="AVAX",order_id="original",did="owner")
    costs=staged_cost_evidence(rows,**kw)
    rates={(1,a,"ETH"):"2000",(43114,b,"AVAX"):"30"}
    assert historical_cost_usd(costs,rates)==Fraction(9)
    assert historical_cost_usd(costs,{(1,a,"ETH"):"2000"}) is None
    with pytest.raises(ValueError):
        staged_cost_evidence([rows[0],rows[0]],**kw)
    with pytest.raises(ValueError):
        staged_cost_evidence([dict(rows[0],proof={"source_hash":a,"state":"BRIDGE_CREDIT_VERIFIED","source_gas_atomic":"0"})],**kw)

def test_fee_rounds_down_to_settlement_atomic_not_above_five_percent():
    r=calculate_realized_net([fill("a","BUY","1","1",network_cost_usd="0"),
        fill("b","SELL","1","1.000001",network_cost_usd="0")])[0]
    assert r["fee_due_stable_atomic"]=="0"
