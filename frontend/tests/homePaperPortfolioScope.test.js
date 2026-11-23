import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
  HOME_PAPER_PORTFOLIO_COINS,summarizeHomePaperPortfolio,
  readObservedPaperEquity,appendObservedPaperEquity,saveObservedPaperEquity,
  paperSparkline,
} from '../src/paperPortfolio.js';

const did='did:privy:portfolio-user-a';
const second='did:privy:portfolio-user-b';
const fakeStore=()=>{
  const data=new Map();
  return {
    getItem:key=>data.get(key)??null,
    setItem:(key,val)=>data.set(key,val),
    data,
  };
};
const summary=(equity,startingCapital=46600)=>({
  mode:'PAPER',valuationAvailable:true,scope:'ALL',
  equity,startingCapital,pnl:equity-startingCapital,
});
const t0=Math.floor(Date.now()/120000)*120000+1000;

test('ALL summary combines BTC ETH and other started paper coins, not a BTC price line',()=>{
  const coreRow=(asset,equity)=>({
    asset,status:'ACTIVE',paper_only:true,real_trading_enabled:false,
    wallet_funds_used:false,demo_start_cash:'1000',
    position:{coin_quantity:'0.1',stable_cash:'0',
      valuation:{quote_valid:true,estimated_total_equity_usd:equity}},
  });
  const core={BTC:coreRow('BTC',900),ETH:coreRow('ETH',1000)};
  const assets=HOME_PAPER_PORTFOLIO_COINS.slice(2).map(asset=>(
    asset==='BNB'?{asset,status:'ACTIVE',budget_usd:1000,
      estimated_equity_usd:1200,quote_valid:true}
      :{asset,status:'NOT_STARTED'}
  ));
  const extra={paper_only:true,simulation_only:true,
    real_trading_enabled:false,wallet_funds_used:false,assets};
  const total=summarizeHomePaperPortfolio(core,extra,'ALL');
  assert.equal(total.equity,3100);
  assert.equal(total.startingCapital,3000);
  assert.equal(total.pnl,100);
  assert.equal(total.assetCount,3);
});

test('actual virtual portfolio history survives OTA/reload, isolated by account',()=>{
  const storage=fakeStore();
  let samples=appendObservedPaperEquity([],summary(39747.27),t0);
  samples=appendObservedPaperEquity(samples,summary(39748.11),t0+20000);
  assert.equal(samples.length,1); // latest real value within same time bucket
  assert.equal(samples[0].value,39748.11);
  samples=appendObservedPaperEquity(samples,summary(39738.11),t0+130000);
  assert.equal(samples.length,2);
  assert.equal(saveObservedPaperEquity(did,'ALL',samples,storage),true);
  assert.deepEqual(readObservedPaperEquity(did,'ALL',storage,t0+130000),samples);
  assert.deepEqual(readObservedPaperEquity(second,'ALL',storage,t0+130000),[]);
  assert.deepEqual(readObservedPaperEquity(did,'BTC',storage,t0+130000),[]);
});

test('different configured capital and aged samples never produce a false bridge',()=>{
  const storage=fakeStore();
  const previous=[
    {at:t0-25*3600000,value:50000,capital:46600},
    {at:t0-180000,value:40000,capital:46600},
  ];
  assert.equal(saveObservedPaperEquity(did,'ALL',previous,storage),true);
  const restored=readObservedPaperEquity(did,'ALL',storage,t0);
  assert.equal(restored.length,1);
  const changed=appendObservedPaperEquity(restored,summary(8000,10000),t0+130000);
  assert.equal(changed.length,1);
  assert.equal(changed[0].capital,10000);
});

test('short-term rise cannot turn a cumulatively negative PAPER portfolio green',()=>{
  const samples=[{at:t0,value:39740,capital:46600},
    {at:t0+130000,value:39747.27,capital:46600}];
  const spark=paperSparkline(samples);
  assert.equal(spark.positive,true); // recent change != all-time PnL
  const card=readFileSync(new URL('../src/CompactOverview.jsx',import.meta.url),'utf8');
  const app=readFileSync(new URL('../src/App.jsx',import.meta.url),'utf8');
  assert.match(card,/const color=paperMode\?\(positive\?'#5bf2b6':'#ff9388'\)/);
  assert.match(card,/stroke=\{color\}/);
  assert.match(card,/fill=\{color\}/);
  assert.match(app,/readObservedPaperEquity\(did,scope\)/);
  assert.match(app,/appendObservedPaperEquity\(/);
  assert.match(app,/saveObservedPaperEquity\(did,scope,state\.samples\)/);
});
