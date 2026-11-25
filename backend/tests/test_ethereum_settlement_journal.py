import pytest
from backend.ethereum_settlement_journal import validate_quote_terms

def test_quote_bound_to_ethereum_and_exact_atomic_amounts():
    terms = dict(asset="POL", side="SELL", stable="USDC",
        wallet_address="0x"+"12"*20, source_token="0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
        destination_token="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", source_chain_id=137,
        destination_chain_id=1, input_atomic="25000000000000000000",
        minimum_output_atomic="1000000", issued_at=100, expires_at=130)
    assert validate_quote_terms(terms) == terms
    for change in ({"destination_chain_id":137}, {"input_atomic":"0"},
                   {"minimum_output_atomic":"1.5"}, {"expires_at":131},
                   {"wallet_address":"not a wallet"},{"source_token":"0x"+"34"*20},{"destination_token":"0x"+"56"*20}):
        with pytest.raises(ValueError):
            validate_quote_terms({**terms, **change})


def test_bridge_strategy_inventory_excludes_manual_buys_and_accounts_for_sales():
    from backend.ethereum_settlement_auto_plan import strategy_owned_bridge_position
    rows=[
        dict(side="BUY",amount_atomic=900,automatic=False),
        dict(side="BUY",amount_atomic=100,automatic=True),
        dict(side="SELL",amount_atomic=30,automatic=False),
        dict(side="BUY",amount_atomic=50,automatic=True),
        dict(side="SELL",amount_atomic=20,automatic=True),
    ]
    assert strategy_owned_bridge_position(rows)==100
    assert strategy_owned_bridge_position(rows+[dict(side="SELL",amount_atomic=200,automatic=False)])==0
