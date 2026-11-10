import test from 'node:test';
import assert from 'node:assert/strict';
import {COINS,coinBySymbol,coinMarketSnapshot} from '../src/coins.js';

test('eight native coins preserve approved compact 4x2 layout order',()=>{
  assert.deepEqual(COINS.map(c=>c.symbol),
    ['BTC','ETH','BNB','POL','SOL','XRP','ADA','TRX']);
  assert.equal(COINS.length,8);
  assert.equal(new Set(COINS.map(c=>c.network)).size,8);
  assert.equal(coinBySymbol('POL').network,'polygon-pos-mainnet');
  assert.equal(coinBySymbol('DOGE'),null);
  assert.equal(coinBySymbol('USDC'),null);
  assert(COINS.every(c=>typeof c.glyph==='string'&&c.glyph.length>0));
});

test('only existing BTC and ETH verified price feeds may display live quotes',()=>{
  const now=100000;
  const markets=Object.fromEntries(COINS.map(c=>[c.symbol,{
    market_data_valid:true,price_usdt:100,change_24h_pct:1,
  }]));
  for(const coin of COINS){
    const actual=coinMarketSnapshot(coin.symbol,markets,now-5000,now);
    if(coin.symbol==='BTC'||coin.symbol==='ETH'){
      assert.equal(actual,markets[coin.symbol]);
      assert.equal(coin.paperSettingsReady,true);
    }else{
      assert.equal(actual,null,'No fictitious LIVE price for '+coin.symbol);
      assert.equal(coin.paperSettingsReady,false);
    }
  }
});

test('stale missing or invalid source prices never appear live',()=>{
  const now=100000;
  const good={BTC:{market_data_valid:true,price_usdt:97000}};
  assert.equal(coinMarketSnapshot('BTC',good,now-19999,now),good.BTC);
  assert.equal(coinMarketSnapshot('BTC',good,now-20000,now),null);
  assert.equal(coinMarketSnapshot('BTC',good,now+100,now),null);
  assert.equal(coinMarketSnapshot('BTC',good,0,now),null);
  assert.equal(coinMarketSnapshot('BTC',{},now-1000,now),null);
  for(const invalid of [0,-1,'NaN',null,Infinity]){
    assert.equal(coinMarketSnapshot('BTC',{BTC:{
      market_data_valid:true,price_usdt:invalid,
    }},now-1000,now),null);
  }
  assert.equal(coinMarketSnapshot('ETH',{
    ETH:{market_data_valid:false,price_usdt:2000},
  },now-1000,now),null);
});
