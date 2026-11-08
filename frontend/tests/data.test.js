import test from 'node:test';
import assert from 'node:assert/strict';
import {cleanNumber,ensurePaperOnly,validMarket,portfolioValue,limitHistory,formatNumber,formatSigned} from '../src/data.js';

test('reject real trading and wallet-enabled responses from paper UI',()=>{
  const base={mode:'PAPER_AUTO_DEMO',real_trading_enabled:false,wallet_connected:false};
  assert.deepEqual(ensurePaperOnly(base),base);
  assert.throws(()=>ensurePaperOnly({...base,real_trading_enabled:true}));
  assert.throws(()=>ensurePaperOnly({...base,wallet_connected:true}));
  assert.throws(()=>ensurePaperOnly({...base,mode:'LIVE'}));
});

test('does not display stale price or portfolio valuation',()=>{
  assert.equal(validMarket({market_data_valid:false,price_usdt:5000}),false);
  assert.equal(validMarket({market_data_valid:true,price_usdt:5000}),true);
  assert.equal(validMarket({market_data_valid:true,price_usdt:0}),false);
  assert.equal(portfolioValue({mark_after_estimated_exit_costs:100,market_data_valid:false}),null);
  assert.equal(portfolioValue({mark_after_estimated_exit_costs:100,market_data_valid:true}),100);
});

test('unknown prices and PnL never become zero',()=>{
  assert.equal(cleanNumber(null),null);
  assert.equal(cleanNumber(undefined),null);
  assert.equal(formatNumber(null),'—');
  assert.equal(formatSigned(null),'—');
  assert.equal(cleanNumber('NaN'),null);
});

test('chart respects requested history window',()=>{
  const now=2000;
  const out=limitHistory([{timestamp:10,price_usdt:12},{timestamp:1600,price_usdt:14},{timestamp:1990,price_usdt:15}],300,now);
  assert.deepEqual(out,[{timestamp:1990,price_usdt:15}]);
});

test('charts cap point count without dropping latest tick',()=>{
  const arr=Array.from({length:4000},(_,i)=>({timestamp:10000+i,price_usdt:100+i}));
  const out=limitHistory(arr,6000,14000);
  assert(out.length<=251);
  assert.equal(out.at(-1).timestamp,13999);
});
