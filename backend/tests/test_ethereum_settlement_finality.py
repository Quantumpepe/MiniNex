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
