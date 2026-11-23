import test from 'node:test';
import assert from 'node:assert/strict';
import {EVM_BALANCE_ASSETS,EVM_BALANCE_NETWORKS,EVM_BALANCE_CHAIN_IDS,
  WRAPPED_EVM_ASSETS,validateMulticoinBalanceResponse} from '../src/multicoinBalanceValidation.js';

function sample(){
  return {mode:'REAL_MAINNET_BALANCES_READ_ONLY',wallet_ownership_verified:true,
    real_trading_enabled:false,execution_available:false,wallet_funds_used_for_paper:false,
    asset_order:EVM_BALANCE_ASSETS,
    assets:Object.fromEntries(EVM_BALANCE_ASSETS.map(asset=>
      [asset,{asset,chain_id:EVM_BALANCE_CHAIN_IDS[asset],network:EVM_BALANCE_NETWORKS[asset],decimals:18,native:!['LINK','QNT','UNI','ARB'].includes(asset),
        wallet_funds_used_for_paper:false,status:'verified',balance:'1.000000000000000001'}]))};
}
test('seven additional holdings preserve exact units and partial availability',()=>{
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
    d=>{d.assets.ARB.chain_id=1;},d=>{d.assets.ARB.network='ethereum-mainnet';},
    d=>{d.assets.LINK.decimals=6;},d=>{d.assets.UNI.native=true;},d=>{d.assets.ARB.native=true;},
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

test('exact on-chain wrapped holdings are verified per immutable chain and contract',()=>{
  const data=sample();
  data.wrapped_assets=Object.fromEntries(
    Object.entries(WRAPPED_EVM_ASSETS).map(([asset,info])=>[asset,{
      asset,underlying:info.underlying,network:info.network,
      chain_id:info.chainId,contract:info.contract,decimals:18,
      native:false,status:'verified',balance:'25.000000000000000000',
      wallet_funds_used_for_paper:false,
    }]));
  assert.equal(validateMulticoinBalanceResponse(data),true);
  const invalid=[x=>{x.wrapped_assets.WPOL.chain_id=56;},
    x=>{x.wrapped_assets.WPOL.contract=x.wrapped_assets.WBNB.contract;},
    x=>{x.wrapped_assets.WPOL.decimals=6;},
    x=>{x.wrapped_assets.WPOL.native=true;},
    x=>{x.wrapped_assets.WPOL.status='unavailable';},
    x=>{x.wrapped_assets.WPOL.balance='25e18';}];
  for(const change of invalid){
    const copy=structuredClone(data);
    change(copy);
    assert.equal(validateMulticoinBalanceResponse(copy),false);
  }
  data.wrapped_assets.WPOL.status='unavailable';
  data.wrapped_assets.WPOL.balance=null;
  assert.equal(validateMulticoinBalanceResponse(data),true);
  delete data.wrapped_assets.WPOL;
  assert.equal(validateMulticoinBalanceResponse(data),false);
});
