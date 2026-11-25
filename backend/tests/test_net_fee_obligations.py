from backend.realized_net_accounting import calculate_realized_net
from backend.net_fee_obligations import obligation_evidence

def row(key,side,source,dest,**kw):
    return dict(asset="LINK",family="ETHEREUM",coin_wallet="original-wallet",fill_key=key,
        side=side,stable="USDC",source_amount=source,destination_amount=dest,automatic=True,
        stable_usd="1",network_cost_usd="2") | kw

def test_original_fee_is_bound_to_exact_fifo_lots_without_hwm():
    rows=[row("buy1","BUY","100","2"),row("buy2","BUY","60","1"),
          row("sell1","SELL","1","40"),row("sell2","SELL","2","130")]
    loss,profit=calculate_realized_net(rows)
    assert obligation_evidence(loss) is None
    evidence,digest=obligation_evidence(profit)
    assert evidence["fee_due_stable_atomic"]=="750000"
    assert evidence["chain_id"]==1
    assert evidence["consumed_buy_lots"]==[
        {"fill_key":"buy1","quantity_ratio":["1","1"],"stable":"USDC"},
        {"fill_key":"buy2","quantity_ratio":["1","1"],"stable":"USDC"}]
    assert len(digest)==64
    assert obligation_evidence(profit)==(evidence,digest)
    altered=dict(profit,coin_wallet="different-wallet")
    assert obligation_evidence(altered)[1]!=digest

def test_no_obligation_for_manual_or_unpriced_trade():
    rows=[row("buy","BUY","100","1",network_cost_usd=None),row("sell","SELL","1","150")]
    assert obligation_evidence(calculate_realized_net(rows)[0]) is None
    assert calculate_realized_net([row("buy","BUY","100","1"),
        row("sell","SELL","1","150",automatic=False)])==[]
