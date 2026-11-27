from fractions import Fraction
from backend.portfolio_return import summarize_realized_return

def fill(side, key, source, destination, cost, **kwargs):
    return dict(asset="ETH", family="ETHEREUM", coin_wallet="wallet",
                side=side, stable="USDC", automatic=True, fill_key=key,
                source_amount=source, destination_amount=destination,
                stable_usd=Fraction(1), network_cost_usd=cost, **kwargs)

def test_realized_return_includes_network_costs_and_profit_fee():
    rows=[fill("BUY","b","100","1",Fraction(1)),
          fill("SELL","s","1","112",Fraction(1))]
    result=summarize_realized_return(rows)
    assert result["pnl_usd"] == "9.500000000000"
    assert result["pnl_pct"] == "9.40594059"
    assert result["verified_closes"] == 1
    assert result["pending_closes"] == 0

def test_unmatched_and_unpriced_sells_are_not_reported_as_zero():
    result=summarize_realized_return([fill("SELL","s","1","10",None)])
    assert result["pnl_usd"] is None
    assert result["pending_closes"] == 1

def test_no_closed_trades_no_realized_gain():
    result=summarize_realized_return([fill("BUY","b","100","1",Fraction(1))])
    assert result["pnl_usd"] == "0.000000000000"
    assert result["verified_closes"] == 0
