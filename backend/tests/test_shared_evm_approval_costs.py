import pytest
from backend.shared_evm_approval_costs import plan_approval_costs


def approval(block,side="BUY",suffix="ab"):
    return dict(side=side,stable="USDC",chain_id=1,tx_hash="0x"+suffix*32,
        status="FINALIZED_SUCCESS",confirmed_gas_atomic=10**14,
        gas_block_number=block,gas_block_hash="0x"+"cd"*32,gas_block_unix=1000+block)


def fill(block,side):
    return dict(side=side,stable="USDC",chain_id=1,block_number=block)


def test_cost_assigned_once_and_future_approval_not_added_to_old_trade():
    rows=[approval(9),approval(19,"SELL","ef"),approval(21,"BUY","aa")]
    assigned,pending=plan_approval_costs(rows,[fill(10,"BUY"),fill(20,"SELL")])
    assert [i for i,r in assigned]==[0,1]
    assert pending==1


def test_unknown_cost_or_ambiguous_order_cannot_report_net():
    for bad in [dict(approval(9),confirmed_gas_atomic=None),approval(10)]:
        with pytest.raises(ValueError):
            plan_approval_costs([bad],[fill(10,"BUY")])
    with pytest.raises(ValueError):
        plan_approval_costs([approval(9),approval(9)],[fill(10,"BUY")])
