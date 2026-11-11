import test from 'node:test';
import assert from 'node:assert/strict';
import {COINS,coinBySymbol,coinMarketSnapshot} from '../src/coins.js';

test('twelve coins preserve approved compact 4x3 layout order',()=>{
  assert.deepEqual(COINS.map(c=>c.symbol),
    ['BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ADA']);
  assert.equal(COINS.length,12);
  assert.equal(new Set(COINS.map(c=>c.network)).size,10);
  assert.equal(coinBySymbol('POL').network,'polygon-pos-mainnet');
  assert.equal(coinBySymbol('DOGE').network,'dogecoin-mainnet');
  assert.equal(coinBySymbol('ADA').inProgress,true);
  assert.equal(coinBySymbol('LINK').token,true);
  assert.equal(coinBySymbol('UNI').token,true);
  assert.equal(coinBySymbol('USDC'),null);
  assert(COINS.every(c=>typeof c.glyph==='string'&&c.glyph.length>0));
});

test('all twelve verified read-only prices can display without enabling coin trading',()=>{
  const now=100000;
  const markets=Object.fromEntries(COINS.map(c=>[c.symbol,{
    market_data_valid:true,price_usdt:100,change_24h_pct:1,
    market_data_only:!c.paperSettingsReady,trading_enabled:false,
  }]));
  for(const coin of COINS){
    const quote=coinMarketSnapshot(coin.symbol,markets,now-5000,now);
    assert.equal(quote,markets[coin.symbol]);
    assert.equal(coin.paperSettingsReady,['BTC','ETH'].includes(coin.symbol));
    assert.equal(quote.trading_enabled,false);
  }
  assert.equal(coinMarketSnapshot('UNKNOWN',markets,now-5000,now),null);
  assert.equal(coinMarketSnapshot('POL',{POL:{market_data_valid:false,price_usdt:100}},now-5000,now),null);
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
