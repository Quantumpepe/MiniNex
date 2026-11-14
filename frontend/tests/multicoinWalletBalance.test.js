import test from 'node:test';
import assert from 'node:assert/strict';
import {EVM_BALANCE_ASSETS,validateMulticoinBalanceResponse}
  from '../src/multicoinBalanceValidation.js';

const network={BNB:'bsc-mainnet',POL:'polygon-pos-mainnet',AVAX:'avalanche-c-mainnet',LINK:'ethereum-mainnet',UNI:'ethereum-mainnet'};
function fixture(){
  const assets=Object.fromEntries(EVM_BALANCE_ASSETS.map(asset=>[asset,{
    asset,network:network[asset],status:'verified',balance:'1.25',
    native:!['LINK','UNI'].includes(asset),
    wallet_funds_used_for_paper:false,
  }]));
  return {
    mode:'REAL_MAINNET_BALANCES_READ_ONLY', wallet_ownership_verified:true,
    wallet_funds_used_for_paper:false, real_trading_enabled:false,
    execution_available:false, asset_order:[...EVM_BALANCE_ASSETS],assets,
  };
}

test('five actual EVM balances render per network without merging simulated accounts',()=>{
  assert.equal(validateMulticoinBalanceResponse(fixture()),true);
  const x=fixture();
  x.assets.LINK.balance='0';
  x.assets.UNI={...x.assets.UNI,balance:null,status:'unavailable'};
  assert.equal(validateMulticoinBalanceResponse(x),true);
});
test('network mixups, forged coins and decimal amounts are rejected',()=>{
  const wrong=fixture();wrong.assets.POL.network='ethereum-mainnet';
  assert.equal(validateMulticoinBalanceResponse(wrong),false);
  const invalid=fixture();invalid.assets.UNI.balance='NaN';
  assert.equal(validateMulticoinBalanceResponse(invalid),false);
  const absent=fixture();absent.assets.BNB.balance=null;
  assert.equal(validateMulticoinBalanceResponse(absent),false);
  const dollar=fixture();dollar.assets.LINK.balance='$100';
  assert.equal(validateMulticoinBalanceResponse(dollar),false);
});
test('never accept actual funds as paper capital or trade authorization',()=>{
  for(const [field,value] of [
    ['wallet_funds_used_for_paper',true],['real_trading_enabled',true],
    ['execution_available',true],['wallet_ownership_verified',false],
  ]){
    const data=fixture();data[field]=value;
    assert.equal(validateMulticoinBalanceResponse(data),false);
  }
  const data=fixture();data.assets.BNB.wallet_funds_used_for_paper=true;
  assert.equal(validateMulticoinBalanceResponse(data),false);
});
