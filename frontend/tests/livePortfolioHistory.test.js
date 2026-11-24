import test from 'node:test';
import assert from 'node:assert/strict';
import {readLiveEquity,appendLiveEquity,saveLiveEquity} from '../src/livePortfolioHistory.js';
import {paperSparkline} from '../src/paperPortfolio.js';

test('live history persists exact valuation observations separately per owner',()=>{
  const map=new Map();
  const storage={getItem:k=>map.get(k),setItem:(k,v)=>map.set(k,v)};
  const owner='did:privy:one',now=Math.floor(Date.now()/30000)*30000+1000;
  const summary=value=>({mode:'LIVE_WALLET',valuationAvailable:true,incomplete:false,coverage:'POL:full|XRP:full',observedEquityUsd:value});
  let points=appendLiveEquity([],summary(14.161),now);
  points=appendLiveEquity(points,summary(14.162),now+10000);
  assert.equal(points.length,1);
  points=appendLiveEquity(points,summary(14.163),now+30000);
  points=appendLiveEquity(points,summary(14.158),now+60000);
  assert.equal(points.length,3);
  const partial=appendLiveEquity(points,{...summary(8),incomplete:true,coverage:'POL:full|XRP:unknown'},now+90000);
  assert.equal(partial.length,4);
  assert.equal(paperSparkline(partial,{allowSingle:true}).count,1);
  assert.equal(paperSparkline(partial,{allowSingle:true}).area,'');
  assert.equal(saveLiveEquity(owner,points,storage),true);
  assert.deepEqual(readLiveEquity(owner,storage,now+60000),points);
  assert.deepEqual(readLiveEquity('did:privy:two',storage,now+60000),[]);
  assert.deepEqual(readLiveEquity(owner,storage,now+25*3600000),[]);
  const chart=paperSparkline(points);
  assert.ok(chart.line.includes('L'));
  assert.equal(chart.positive,false);
});
