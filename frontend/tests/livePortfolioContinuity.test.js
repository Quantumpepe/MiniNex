import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {combineVerifiedNativeAndWrapped,
  summarizeVerifiedWalletPortfolio} from '../src/walletPortfolio.js';
import {EVM_BALANCE_ASSETS,EVM_BALANCE_NETWORKS,EVM_BALANCE_CHAIN_IDS,
  WRAPPED_EVM_ASSETS} from '../src/multicoinBalanceValidation.js';

const balance=x=>({status:'verified',balance:x});

test('wrapped POL remains 25 POL of wallet value after native wrap, not a USDC sale',()=>{
  const wrapped=balance('25.000000000000000001');
  const native=balance('0.123456789000000000');
  const total=combineVerifiedNativeAndWrapped(native,wrapped);
  assert.equal(total.status,'verified');
  assert.equal(total.balance,'25.123456789000000001');
  assert.equal(total.incomplete,false);
  assert.equal(combineVerifiedNativeAndWrapped(balance('0'),wrapped).balance,
    wrapped.balance);
  assert.equal(combineVerifiedNativeAndWrapped(native,null).incomplete,true);
  assert.equal(combineVerifiedNativeAndWrapped(null,null).status,'unavailable');
});

test('real portfolio counts on-chain WPOL under POL once, not as a stablecoin',()=>{
  const nativeAssets=Object.fromEntries(EVM_BALANCE_ASSETS.map(asset=>[asset,{
    asset,chain_id:EVM_BALANCE_CHAIN_IDS[asset],network:EVM_BALANCE_NETWORKS[asset],
    decimals:18,native:!['LINK','QNT','UNI','ARB'].includes(asset),
    wallet_funds_used_for_paper:false,status:'verified',balance:'0',
  }]));
  const wrapped=Object.fromEntries(Object.entries(WRAPPED_EVM_ASSETS).map(
    ([asset,info])=>[asset,{asset,underlying:info.underlying,network:info.network,
      chain_id:info.chainId,contract:info.contract,decimals:18,native:false,
      status:'verified',balance:asset==='WPOL'?'25':'0',
      wallet_funds_used_for_paper:false}]));
  const source={evm:{
    mode:'REAL_MAINNET_BALANCES_READ_ONLY',wallet_ownership_verified:true,
    wallet_funds_used_for_paper:false,real_trading_enabled:false,
    execution_available:false,asset_order:EVM_BALANCE_ASSETS,
    assets:nativeAssets,wrapped_assets:wrapped,
  }};
  const now=Date.now();
  const prices={POL:{market_data_valid:true,price_usdt:'0.2'}};
  const result=summarizeVerifiedWalletPortfolio(source,prices,now-1000,now);
  assert.equal(result.mode,'LIVE_WALLET');
  assert.equal(result.equity,5);
  assert.equal(result.holdings.length,1);
  assert.deepEqual(result.holdings.map(x=>x.asset),['POL']);
  assert.equal(result.holdings[0].balance,'25');
  assert.equal(result.holdings[0].value,5);
  assert.equal(result.incomplete,true); // BTC/ETH/native wallet sources missing
  source.evm.wrapped_assets.WPOL.status='unavailable';
  source.evm.wrapped_assets.WPOL.balance=null;
  const unreadable=summarizeVerifiedWalletPortfolio(source,prices,now-1000,now);
  assert.equal(unreadable.equity,null);
  assert.equal(unreadable.incomplete,true);
});

test('portfolio does not reset verified values or curve on tab switches',()=>{
  const hook=readFileSync(new URL('../src/useWalletPortfolioHome.js',import.meta.url),'utf8');
  assert.doesNotMatch(hook,/setState\(\{did,sources:null,status:'loading'\}\)/);
  assert.doesNotMatch(hook,/setSamples\(\{did,points:\[\]\}\)/);
  assert.match(hook,/previous.did!==did/);
  assert.match(hook,/previous.did===did/);
  assert.match(hook,/status:previous.sources\?'refreshing':'loading'/);
  assert.match(hook,/status:previous.sources\?'stale':'error'/);
  assert.match(hook,/!visible\|\|!matched\|\|state.status!=='ready'/);
});
