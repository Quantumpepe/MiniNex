import asyncio
from backend import net_fee_send as m

def test_net_fee_sender_disabled_until_receipt_reconciliation(monkeypatch):
    assert m.net_fee_worker_enabled() is False
    def forbidden(*a,**k):raise AssertionError("Disabled worker must not query or sign")
    monkeypatch.setattr(m,"pending_net_fee_sends",forbidden)
    monkeypatch.setattr(m,"audit_original_evm_coin",forbidden)
    assert asyncio.run(m.reconcile_net_fee_sends_once(None)) is None
    assert asyncio.run(m.submit_net_fee_once(None,{})) is None


import pytest
from backend.ethereum_settlement_plan import COIN_NETWORKS,ETHEREUM_STABLES
from backend.shared_evm_fee_privy_policy_draft import build_owner_evm_fee_policy_draft

@pytest.mark.parametrize("asset",list(COIN_NETWORKS))
@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_all_original_fee_assets_use_ethereum_and_exact_booked_amount(monkeypatch,asset,stable):
    wallet="0x"+"a"*40;treasury="0x"+"b"*40
    monkeypatch.setenv("MININEX_TREASURY_WALLET",treasury)
    now=1800000000
    rule=build_owner_evm_fee_policy_draft(asset="ETH",stable=stable,wallet=wallet,treasury=treasury,
        owner_id="owner_123",executor_id="signer_123",owner_max_fee_atomic="100000000",
        expires_at=now+3600,now=now)["rules"][0]
    rule["name"]=f"MiniNex {asset} {stable} realized net fee Ethereum"
    record=dict(wallet_address=wallet,wallet_id="wallet_123",policy_sha256="a"*64,
        scope=dict(asset=asset,stable=stable,expires_at=now+3600,treasury=treasury,
            net_fee_policy="ETHEREUM_REALIZED_NET_V1",net_fee_atomic_cap="100000000"),
        candidate_json={"rules":[rule]})
    obligation=dict(privy_did="did:privy:owner",asset=asset,family=COIN_NETWORKS[asset][3],
        fill_key="original",stable=stable,amount_atomic=123456,evidence_sha256="b"*64,
        evidence={"coin_wallet":wallet,"fee_due_stable_atomic":"123456","chain_id":1})
    original=m.fee_identity(obligation,record,now=now)
    assert original["tx"]["chainId"]==1
    assert original["tx"]["to"]==ETHEREUM_STABLES[stable]
    assert original["tx"]["data"]=="0xa9059cbb"+format(int(treasury,16),"064x")+format(123456,"064x")
    assert original["body"]["caip2"]=="eip155:1"
    assert m.fee_identity(obligation,record,now=now)==original
    with pytest.raises(ValueError):
        m.fee_identity(dict(obligation,amount_atomic=123457),record,now=now)
