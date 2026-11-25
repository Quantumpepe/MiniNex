import test from 'node:test';
import assert from 'node:assert/strict';
import {validEvmOrderHistory,validEvmFinalizedFills} from '../src/evmLiveOwnerRules.js';

const tx='0x'+'a'.repeat(64),destination='0x'+'b'.repeat(64);
for(const [asset,network] of Object.entries({BNB:56,POL:137,AVAX:43114,ARB:42161})){
  for(const side of ['BUY','SELL']){
    test(asset+' '+side+' history uses the source and destination networks',()=>{
      const chain=side==='BUY'?1:network,dst=side==='BUY'?network:1;
      const common={asset,side,stable:'USDC',chain_id:chain,intent_key:'original-parent',
        tx_hash:tx,created_at:'2026-11-25T12:00:00Z'};
      const order={...common,ethereum_settlement_order:true,status:'FINALIZED_SUCCESS',
        swap_erc20_leg_finalized:true,native_trade_cycle_settled:false,fee_transferred:false};
      const report={mode:'OWNER_DURABLE_EVM_ORDER_STATUS_ONLY',asset,orders:[order],
        includes_simulation:false,wallet_signing_enabled_by_this_api:false,
        automatic_trade_execution_enabled_by_this_api:false,history_truncated:false};
      assert.equal(validEvmOrderHistory(report,asset),true);
      assert.equal(validEvmOrderHistory({...report,orders:[{...order,chain_id:dst}]},asset),false);
      const fill={...common,ethereum_settlement_verified:true,destination_chain_id:dst,
        destination_tx_hash:destination,onchain_finalized:true,native_coin_cycle_complete:false,
        realized_net_usd:null,performance_fee_paid:false,spent_asset:side==='BUY'?'USDC':asset,
        received_asset:side==='BUY'?asset:'USDC',spent_amount:'1',received_amount:'2'};
      const fills={mode:'FINALIZED_EVM_REAL_FILLS_ONLY',asset,fills:[fill],includes_simulation:false,
        wallet_signing_authorized:false,full_native_order_settlement_verified:false,
        realized_net_pnl_calculated:false,performance_fee_settled:false,history_truncated:false};
      assert.equal(validEvmFinalizedFills(fills,asset),true);
      assert.equal(validEvmFinalizedFills({...fills,fills:[{...fill,destination_chain_id:chain}]},asset),false);
    });
  }
}
