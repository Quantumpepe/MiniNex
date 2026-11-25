import pytest
from backend.net_fee_receipts import verify_fee_receipt
from backend.ethereum_settlement_plan import ETHEREUM_STABLES
from backend.realized_net_accounting import calculate_realized_net
from backend.net_fee_obligations import obligation_evidence
from backend.ethereum_settlement_funding import summarize_pending_funding

def example(stable="USDC"):
    wallet="0x"+"a"*40;treasury="0x"+"b"*40;h="0x"+"c"*64;block="0x"+"d"*64
    token=ETHEREUM_STABLES[stable];amount=800000
    row=dict(chain_id=1,stable=stable,wallet_address=wallet,treasury_wallet=treasury,token_address=token,
        tx_hash=h,amount_atomic=amount,evidence_sha256="a"*64,request_digest="b"*64)
    tx=dict(hash=h,blockHash=block,blockNumber="0x10",**{"from":wallet,"to":token,"value":"0x0",
        "input":"0xa9059cbb"+format(int(treasury,16),"064x")+format(amount,"064x")})
    receipt=dict(transactionHash=h,blockHash=block,blockNumber="0x10",status="0x1",gasUsed="0x100",effectiveGasPrice="0x20",
        logs=[dict(address=token,topics=["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
              "0x"+wallet[2:].rjust(64,"0"),"0x"+treasury[2:].rjust(64,"0")],data="0x"+format(amount,"064x"))])
    return row,tx,receipt

@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_exact_finalized_fee_only(stable):
    row,tx,receipt=example(stable)
    proof=verify_fee_receipt(row,tx,receipt)
    assert proof["outcome"]=="PAID_VERIFIED" and proof["amount_atomic"]=="800000"
    assert proof["network_gas_atomic"]=="8192"
    for changed in (dict(tx,to="0x"+"f"*40),dict(tx,input="0x"),dict(tx,value="0x1")):
        with pytest.raises(ValueError):verify_fee_receipt(row,changed,receipt)
    for changed in (dict(receipt,logs=[]),dict(receipt,logs=receipt["logs"]*2)):
        with pytest.raises(ValueError):verify_fee_receipt(row,tx,changed)
    proof=verify_fee_receipt(row,tx,dict(receipt,status="0x0",logs=[]))
    assert proof["outcome"]=="FINALIZED_REVERTED" and proof["amount_atomic"]=="0"

def test_paid_fee_subtracted_once_and_only_from_original_reserve():
    def fill(key,side,src,dst):
        return dict(asset="LINK",family="ETHEREUM",coin_wallet="original-wallet",fill_key=key,
            side=side,stable="USDC",source_amount=src,destination_amount=dst,automatic=True,
            stable_usd="1",network_cost_usd="2",unit="FINALIZED_PROFIT_INPUT")
    rows=[fill("buy","BUY","100","1"),fill("sell","SELL","1","120")]
    value=calculate_realized_net(rows)[0]
    _,digest=obligation_evidence(value)
    h="0x"+"c"*64
    payment=dict(evidence_sha256=digest,amount_atomic="800000",status="PAID_VERIFIED",tx_hash=h,
                 receipt_outcome="PAID_VERIFIED",receipt_amount_atomic="800000",receipt_hash=h)
    rows[1]["fee_payment"]=payment
    result=summarize_pending_funding(rows,asset="LINK",stable="USDC",stable_usd="1")
    assert result["protected_profit_net_atomic"]=="16000000"
    assert result["protected_fee_due_atomic"]=="0"
    assert result["verified_fee_paid_atomic"]=="800000"
    assert result["pending_wallet_stable_atomic"]=="15200000"
    rows[1]["fee_payment"]=dict(payment,status="SUBMITTED",receipt_outcome=None,receipt_amount_atomic=None,receipt_hash=None)
    assert summarize_pending_funding(rows,asset="LINK",stable="USDC",stable_usd="1")["pending_wallet_stable_atomic"]=="16000000"
    rows[1]["fee_payment"]=dict(payment,receipt_amount_atomic="799999")
    with pytest.raises(ValueError):calculate_realized_net(rows)
