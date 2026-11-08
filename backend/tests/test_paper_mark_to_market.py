"""Personal paper valuations are explicitly estimated and never execute orders."""
from backend.user_paper import add_paper_mark_to_market
from backend.auto_engine import PaperAutoEngine

class Feed:
    def __init__(self, valid=True, price=74000.0):
        self.valid=valid
        self.price=price

    def snapshot(self):
        return {"markets":{"BTC":{"market_data_valid":self.valid,
                "price_usdt":self.price}}}

def active():
    return {"paper_only":True,"asset":"BTC","status":"ACTIVE",
            "position":{"stable_cash":"2.50",
                        "coin_quantity":"0.0066498839313766465",
                        "cost_basis":"497.50","realized_net_profit":"0",
                        "trade_count":1}}

def test_open_position_returns_realistic_estimated_exit_and_unrealized_net():
    original=active()
    result=add_paper_mark_to_market(original,Feed())
    value=result["position"]["valuation"]
    assert value["quote_valid"] is True
    assert 480<value["estimated_exit_net_usd"]<500
    assert value["unrealized_net_pnl_usd"]<0
    assert abs(value["estimated_total_equity_usd"] -
               (value["estimated_exit_net_usd"]+2.5)) < 1e-5
    assert original["position"].get("valuation") is None
    assert result["paper_only"] is True

def test_no_unverified_price_is_presented_as_estimated_pnl():
    value=add_paper_mark_to_market(active(),Feed(valid=False))["position"]["valuation"]
    assert value["quote_valid"] is False
    assert value["unrealized_net_pnl_usd"] is None
    assert value["estimated_total_equity_usd"] is None

def test_zero_position_has_cash_equity_but_not_false_profit():
    state=active()
    state["position"].update({"coin_quantity":"0","cost_basis":"0",
                               "stable_cash":"520"})
    value=add_paper_mark_to_market(state,Feed())["position"]["valuation"]
    assert value["quote_valid"] is True
    assert value["unrealized_net_pnl_usd"]==0.0
    assert value["estimated_total_equity_usd"]==520.0

def test_market_feed_malformed_value_never_displays_fake_profit():
    for bad in (None,-1,float("nan"),"garbage"):
        value=add_paper_mark_to_market(active(),Feed(price=bad))["position"]["valuation"]
        assert value["quote_valid"] is False

def test_non_paper_payload_is_rejected():
    try:
        add_paper_mark_to_market({"paper_only":False},Feed())
    except ValueError:
        return
    assert False


def test_cost_breakdown_reconciles_exactly_without_double_deduction():
    state=active()
    state["trades"]=[{
        "kind":"PAPER_BUY",
        "estimated_costs":{"venue_fee":0.496,"network_fee":2.5,
                           "spread_and_slippage_cost":0.645}
    }]
    val=add_paper_mark_to_market(state,Feed())["position"]["valuation"]
    assert val["last_buy_costs_usd"]==3.641
    assert abs(val["estimated_sale_costs_usd"]-(
        val["estimated_sale_fee_usd"]+
        val["estimated_sale_network_fee_usd"]+
        val["estimated_sale_spread_slippage_usd"])) < 0.000002
    assert abs(val["estimated_sale_gross_usd"]-
        val["estimated_sale_costs_usd"]-val["estimated_exit_net_usd"])<0.000002
    assert abs(val["estimated_exit_net_usd"]-
        497.5-val["unrealized_net_pnl_usd"])<0.000002
    # Buy costs are already part of the 497.50 invested amount.
    assert val["unrealized_net_pnl_usd"] == round(
        val["estimated_exit_net_usd"]-497.5,6)


def test_historical_entry_cost_missing_is_not_fabricated():
    val=add_paper_mark_to_market(active(),Feed())["position"]["valuation"]
    assert val["last_buy_costs_usd"] is None
    assert val["estimated_sale_costs_usd"]>0
