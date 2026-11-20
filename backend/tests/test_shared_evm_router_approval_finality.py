"""One-shot ERC20 Router02 grant: only original chain-finalized approval is usable."""
import pytest

from backend.shared_evm_router_approval_finality import _verify_approval_finality
from backend.shared_evm_router_approval_send import _original_approval_id
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
HASH="0x"+"cd"*32
BLOCK="0x"+"ef"*32

@pytest.mark.parametrize("asset,stable",[
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
    ("LINK","USDC"),("UNI","USDT"),("ARB","USDC"),
])
def test_exact_original_allowance_finality_and_reorg_guards(asset,stable):
    pair=_pair(asset,stable,"BUY")
    row=dict(asset=asset,stable=stable,side="BUY",
        chain_id=pair["chain_id"],wallet_address=WALLET,
        token_address=pair["source_token"].lower(),
        router_address=pair["router"].lower(),
        amount_atomic=12345,tx_hash=HASH)
    tx=dict(hash=HASH,from_=WALLET,to=row["token_address"],
            chainId=pair["chain_id"],value="0x0",
            input="0x095ea7b3"+f"{int(pair['router'],16):064x}"+f"{12345:064x}",
            blockNumber="0x64",blockHash=BLOCK)
    tx["from"]=tx.pop("from_")
    receipt=dict(transactionHash=HASH,**{
        "from":WALLET,"to":row["token_address"],
        "blockNumber":"0x64","blockHash":BLOCK,"status":"0x1",
    })
    check=lambda t,r,final:_verify_approval_finality(
        row,chain_id=hex(pair["chain_id"]),tx=t,receipt=r,
        finalized={"number":final})
    assert check(tx,receipt,"0x63") is None
    assert check(tx,receipt,"0x64")=="FINALIZED_SUCCESS"
    assert check(tx,{**receipt,"status":"0x0"},"0x64")=="FINALIZED_REVERTED"
    with pytest.raises(ValueError):
        check({**tx,"input":"0x095ea7b3"+"f"*128},receipt,"0x64")
    with pytest.raises(ValueError):
        check(tx,{**receipt,"blockHash":"0x"+"de"*32},"0x64")

def test_second_unrelated_market_event_has_independent_approval_nonce():
    args=dict(did="did:privy:user",asset="LINK",side="BUY",
              stable="USDC",wallet=WALLET,amount=123456,revision=5)
    a=_original_approval_id(**args,signal_event_id="market_100")
    b=_original_approval_id(**args,signal_event_id="market_101")
    assert a!=b
    assert a.startswith("appr_") and b.startswith("appr_")
