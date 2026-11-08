import test from 'node:test';
import assert from 'node:assert/strict';
import {compactDecision, compactNetPnL, compactChartData} from '../src/compact.js';
test('Signal labels are translated without inventing a trade',()=>{
 assert.equal(compactDecision('WAIT','de'),'Warten');
 assert.equal(compactDecision('PAPER_BUY','en'),'Buy');
 assert.equal(compactDecision('SELL','de'),'Verkaufen');
 assert.equal(compactDecision(null,'de'),'Warten');
 assert.equal(compactDecision('UNKNOWN','en'),'Wait');
});
test('Net PnL sums realized and estimated unrealized only when available',()=>{
 assert.equal(compactNetPnL({position:'COIN',realized_net_pnl:4,estimated_unrealized_net_pnl:-2.5}),1.5);
 assert.equal(compactNetPnL({position:'STABLE',realized_net_pnl:-3.2}),-3.2);
 assert.equal(compactNetPnL({position:'COIN',realized_net_pnl:0,estimated_unrealized_net_pnl:null}),null);
 assert.equal(compactNetPnL(null),null);
});
test('Compact chart uses genuine supplied history only',()=>{
 assert.deepEqual(compactChartData([],900),[]);
 const a=Array.from({length:180},(_,i)=>({timestamp:1000+i*5,price_usdt:200+i}));
 const xs=compactChartData(a,60);
 assert(xs.length>1 && xs.length<=14);
 assert.equal(xs.at(-1).price_usdt,379);
});
