import test from 'node:test';
import assert from 'node:assert/strict';
import {summarizePaperSessions,paperSparkline} from '../src/paperPortfolio.js';

const row=(asset,cash,coin,equity,start=5000)=>({
  asset,status:'ACTIVE',paper_only:true,real_trading_enabled:false,wallet_funds_used:false,
  demo_start_cash:String(start),
  position:{stable_cash:String(cash),coin_quantity:String(coin),
    valuation:{quote_valid:true,estimated_total_equity_usd:equity}}
});

test('total includes independent BTC and ETH paper equity including estimated exit costs',()=>{
  const x=summarizePaperSessions({
    BTC:row('BTC',3000,0.02,5012,5000),
    ETH:row('ETH',4900,0,4968,5000)
  });
  assert.equal(x.equity,9980);
  assert.equal(x.startingCapital,10000);
  assert.equal(x.pnl,-20);
  assert.equal(x.pnlPct,-0.2);
  assert.equal(x.assetCount,2);
  assert.equal(x.mode,'PAPER');
});

test('only started asset counts; login without a session is not 5k or 10k of phantom funds',()=>{
  assert.equal(summarizePaperSessions({BTC:{status:'NOT_STARTED'},ETH:{status:'NOT_STARTED'}}),null);
  const x=summarizePaperSessions({BTC:row('BTC',4900,0,4900),ETH:{status:'NOT_STARTED'}});
  assert.equal(x.equity,4900);
  assert.equal(x.assetCount,1);
});

test('missing or invalid valuation hides aggregate rather than claiming a wallet balance',()=>{
  const btc=row('BTC',4500,0.02,5000);
  btc.position.valuation.quote_valid=false;
  assert.equal(summarizePaperSessions({BTC:btc}),null);
  const forged=row('ETH',1000,0,1000);
  forged.wallet_funds_used=true;
  assert.equal(summarizePaperSessions({ETH:forged}),null);
  const noPrice=row('BTC',0,1,null);
  assert.equal(summarizePaperSessions({BTC:noPrice}),null);
});

test('sparkline requires actual observed equity snapshots',()=>{
  assert.equal(paperSparkline([]),null);
  assert.equal(paperSparkline([{at:1,value:5000}]),null);
  const result=paperSparkline([{at:1,value:5000},{at:2,value:5008},{at:3,value:4999}]);
  assert.match(result.line,/M/);
  assert.match(result.line,/L/);
  assert.equal(result.positive,false);
});
