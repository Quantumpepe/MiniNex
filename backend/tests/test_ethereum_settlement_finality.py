import pytest
from backend.ethereum_settlement_finality import original_outcome_terms

H="0x"+"12"*32
D="0x"+"34"*32

def test_final_outcome_requires_actual_destination_credit():
    data=dict(source_hash=H,destination_hash=D,received=123,deposit_id="42",gas=100,outcome="FINALIZED")
    assert original_outcome_terms(**data)==(H,D,42,123,100,"FINALIZED")
    for change in ({"received":0},{"destination_hash":None},{"deposit_id":None},{"outcome":"PENDING"}):
        with pytest.raises(ValueError):
            original_outcome_terms(**{**data,**change})

def test_reverted_source_is_not_a_fill():
    assert original_outcome_terms(source_hash=H,destination_hash=None,received=None,
        deposit_id=None,gas=100,outcome="SOURCE_REVERTED")==(H,None,None,None,100,"SOURCE_REVERTED")
    with pytest.raises(ValueError):
        original_outcome_terms(source_hash=H,destination_hash=D,received=1,
            deposit_id="42",gas=100,outcome="SOURCE_REVERTED")


def test_finalized_outcome_restores_exact_credit_and_gas_without_network():
    from backend.ethereum_settlement_finality import verified_outcome_status
    from fastapi import HTTPException
    q=dict(source_chain_id=137,destination_chain_id=1,side="SELL",
        minimum_output_atomic="1000000")
    row=dict(source_chain_id=137,destination_chain_id=1,source_tx_hash=H,
        destination_tx_hash=D,bridge_deposit_id=42,received_atomic=1234567,
        source_gas_atomic=123456789,outcome="FINALIZED")
    got=verified_outcome_status(row,q,H)
    assert got["settlement_verified"] is True
    assert got["received_amount"]=="1.234567"
    assert got["source_fee_native"]=="0.000000000123456789"
    with pytest.raises(HTTPException):
        verified_outcome_status(row,q,D)
    reverted={**row,"outcome":"SOURCE_REVERTED","received_atomic":None,
        "bridge_deposit_id":None,"destination_tx_hash":None}
    got=verified_outcome_status(reverted,q,H)
    assert got["state"]=="SOURCE_REVERTED" and got["settlement_verified"] is False
    assert got["received_amount"] is None
