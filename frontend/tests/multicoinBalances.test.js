import test from 'node:test';
import assert from 'node:assert/strict';
import {EVM_BALANCE_ASSETS,EVM_BALANCE_NETWORKS,EVM_BALANCE_CHAIN_IDS,validateMulticoinBalanceResponse} from '../src/multicoinBalanceValidation.js';

function sample(){
  return {mode:'REAL_MAINNET_BALANCES_READ_ONLY',wallet_ownership_verified:true,
    real_trading_enabled:false,execution_available:false,wallet_funds_used_for_paper:false,
    asset_order:EVM_BALANCE_ASSETS,
    assets:Object.fromEntries(EVM_BALANCE_ASSETS.map(asset=>
      [asset,{asset,chain_id:EVM_BALANCE_CHAIN_IDS[asset],network:EVM_BALANCE_NETWORKS[asset],decimals:18,native:!['LINK','UNI'].includes(asset),
        wallet_funds_used_for_paper:false,status:'verified',balance:'1.000000000000000001'}]))};
}
test('five additional holdings preserve exact units and partial availability',()=>{
  const data=sample();
  assert.equal(validateMulticoinBalanceResponse(data),true);
  data.assets.LINK.balance='0';
  data.assets.POL.balance=null;
  data.assets.POL.status='unavailable';
  assert.equal(validateMulticoinBalanceResponse(data),true);
  assert.equal(data.assets.LINK.balance,'0');
});
test('incorrect networks, units and fabricated availability are rejected',()=>{
  for(const change of [
    d=>{d.assets.POL.chain_id=1;},d=>{d.assets.AVAX.network='avalanche-x-mainnet';},
    d=>{d.assets.LINK.decimals=6;},d=>{d.assets.UNI.native=true;},
    d=>{d.assets.BNB.balance=1;},d=>{d.assets.BNB.balance='1e18';},
    d=>{d.assets.BNB.balance='-1';},d=>{d.assets.BNB.balance='0.0000000000000000001';},
    d=>{d.assets.POL.status='unavailable';},d=>{delete d.assets.UNI;},
    d=>{d.wallet_ownership_verified=false;},d=>{d.execution_available=true;},
    d=>{d.real_trading_enabled=true;},d=>{d.wallet_funds_used_for_paper=true;},
  ]){
    const data=sample();change(data);
    assert.equal(validateMulticoinBalanceResponse(data),false);
  }
});
