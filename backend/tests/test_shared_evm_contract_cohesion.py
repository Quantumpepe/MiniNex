"""Prevent fragmented EVM allowlists from silently excluding one coin.

Cross-module contract: every public EVM readback must recognize all seven
configured EVM assets. No signing or blockchain actions occur in this suite.
"""
import importlib
from datetime import datetime,timezone
import pytest

ALL_EVM=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"))
EXTRA_RUNTIME=ALL_EVM-{"ETH"}
EXTRA_LEGACY=EXTRA_RUNTIME-{"QNT"}
ERC20=frozenset(("LINK","QNT","UNI","ARB"))

@pytest.mark.parametrize("module,attr,expected",[
  ("shared_evm_fifo","_ASSETS",ALL_EVM),
  ("shared_evm_trade_cycle_proof","_EVM",ALL_EVM),
  ("shared_evm_permission_status","_ASSETS",EXTRA_LEGACY),
  ("shared_evm_owner_consent","_ADDITIONAL",EXTRA_LEGACY),
  ("shared_evm_auto_sessions","_ASSETS",ALL_EVM),
  ("shared_evm_live_worker","_ASSETS",ALL_EVM),
  ("shared_evm_remote_grant_audit","_ASSETS",EXTRA_LEGACY),
  ("shared_evm_passive_settlement","_SUPPORTED",ALL_EVM),
  ("shared_evm_token_net_reconciliation","_ASSETS",ERC20),
])
def test_every_production_evm_reader_agrees_on_enabled_coins(module,attr,expected):
    mod=importlib.import_module("backend."+module)
    assert frozenset(getattr(mod,attr))==expected, (module,attr)

def test_arb_network_uses_erc20_swap_not_native_wrap():
    from backend.shared_evm_execution import chain_for_asset
    from backend.shared_evm_trade_cycle_proof import _NATIVE
    from backend.shared_evm_swap_receipts import _pair
    assert chain_for_asset("ARB")==(42161,"arbitrum-one-mainnet")
    buy=_pair("ARB","USDC","BUY")
    sell=_pair("ARB","USDC","SELL")
    assert buy["chain_id"]==sell["chain_id"]==42161
    assert "ARB" not in _NATIVE
    assert sell["source_token"]==buy["output_token"]

def test_arb_readiness_and_gross_preview_must_never_claim_payout():
    from backend.shared_evm_permission_status import assess_evm_permission_record
    from backend.shared_evm_fifo import evm_fifo_gross_preview
    p=assess_evm_permission_record(None,asset="ARB",
                                   now=datetime(2026,11,19,tzinfo=timezone.utc))
    assert p["status"]=="NOT_PREPARED" and p["can_submit"] is False
    preview=evm_fifo_gross_preview({
       "asset":"ARB","mode":"FINALIZED_EVM_REAL_FILLS_ONLY",
       "includes_simulation":False,"history_truncated":False,"fills":[]
    })
    assert preview["asset"]=="ARB"
    assert preview["verified_net_pnl_usd"] is None
    assert preview["fee_paid_onchain"] is False
