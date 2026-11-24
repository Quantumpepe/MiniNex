import pytest
from backend.ethereum_settlement_journal import validate_quote_terms

def test_quote_bound_to_ethereum_and_exact_atomic_amounts():
    terms = dict(asset="POL", side="SELL", stable="USDC",
        wallet_address="0x"+"12"*20, source_token="0x"+"34"*20,
        destination_token="0x"+"56"*20, source_chain_id=137,
        destination_chain_id=1, input_atomic="25000000000000000000",
        minimum_output_atomic="1000000", issued_at=100, expires_at=130)
    assert validate_quote_terms(terms) == terms
    for change in ({"destination_chain_id":137}, {"input_atomic":"0"},
                   {"minimum_output_atomic":"1.5"}, {"expires_at":131},
                   {"wallet_address":"not a wallet"}):
        with pytest.raises(ValueError):
            validate_quote_terms({**terms, **change})
