import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {NATIVE_COINS,validNativeBalanceResponse} from '../src/nativeBalanceValidation.js';

const network={SOL:'solana-mainnet',XRP:'xrpl-mainnet',TRX:'tron-mainnet',DOGE:'dogecoin-mainnet'};
function response(){
  return {
    mode:'NATIVE_MAINNET_BALANCES_READ_ONLY',
    wallet_ownership_verified:true,
    wallet_funds_used_for_paper:false,
    real_trading_enabled:false,
    execution_available:false,
    asset_order:[...NATIVE_COINS],
    assets:Object.fromEntries(NATIVE_COINS.map(coin=>[coin,{
      asset:coin,network:network[coin],native:true,
      status:'verified',balance:'1.25',wallet_funds_used_for_paper:false,
    }])),
  };
}

test('wallet accepts independently reported SOL, XRP, TRX and DOGE on mainnet',()=>{
  assert.deepEqual(NATIVE_COINS,['SOL','XRP','TRX','DOGE']);
  assert.equal(validNativeBalanceResponse(response()),true);
  const data=response();
  data.assets.XRP={...data.assets.XRP,status:'wallet_missing',balance:null};
  data.assets.TRX={...data.assets.TRX,status:'unavailable',balance:null};
  data.assets.DOGE={...data.assets.DOGE,status:'wallet_missing',balance:null};
  assert.equal(validNativeBalanceResponse(data),true);
});

test('native wallet response cannot mix networks, paper balances or fake zeros',()=>{
  for(const mutate of [
    d=>{d.asset_order.reverse();},
    d=>{d.assets.XRP.network='ethereum-mainnet';},
    d=>{d.assets.TRX.status='unavailable';d.assets.TRX.balance='0';},
    d=>{d.assets.SOL.balance='NaN';},
    d=>{d.assets.SOL.native=false;},
    d=>{d.assets.TRX.wallet_funds_used_for_paper=true;},
    d=>{d.assets.DOGE.network='bitcoin-mainnet';},
    d=>{d.assets.DOGE.status='wallet_missing';d.assets.DOGE.balance='0';},
    d=>{d.real_trading_enabled=true;},
    d=>{d.execution_available=true;},
    d=>{d.wallet_ownership_verified=false;},
  ]){
    const data=response();mutate(data);
    assert.equal(validNativeBalanceResponse(data),false);
  }
});

test('native balance UI is inside existing wallet and does not replace BTC or EVM',()=>{
  const ui=readFileSync(new URL('../src/WalletBalances.jsx',import.meta.url),'utf8');
  assert.match(ui,/\/api\/wallet\/balances/);
  assert.match(ui,/\/api\/wallet\/multicoin-balances/);
  assert.match(ui,/\/api\/wallet\/native-balances/);
  assert.match(ui,/validNativeBalanceResponse\(data\)/);
  assert.match(ui,/NATIVE_COINS\.map\(asset=>/);
  assert.match(ui,/wallet_missing/);
  assert.match(ui,/nativeStatus==='failed'/);
  assert.match(ui,/getAccessToken,userId,request/);
  assert.doesNotMatch(ui,/mininex-real-trading-activate/);
});
