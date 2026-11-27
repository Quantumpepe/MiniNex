import test from 'node:test';
import assert from 'node:assert/strict';
import {readLiveEquity,appendLiveEquity,saveLiveEquity,livePortfolioSparkline} from '../src/livePortfolioHistory.js';
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

test('LIVE chart keeps cent changes small and uses a fixed time range',()=>{
  const points=[{at:0,value:12,coverage:'XRP:full'},
    {at:30000,value:12.01,coverage:'XRP:full'}];
  const chart=livePortfolioSparkline(points);
  const coords=[...chart.line.matchAll(/[ML]([\d.]+) ([\d.]+)/g)].map(m=>m.slice(1).map(Number));
  assert.equal(chart.count,2);
  assert.equal(coords[1][0],192);
  assert.ok(coords[1][0]-coords[0][0]<7);
  assert.ok(Math.abs(coords[1][1]-coords[0][1])<1.1);
  assert.ok(livePortfolioSparkline([{at:0,value:12,coverage:'XRP:full'},
    {at:30000,value:15,coverage:'XRP:full'}]).lastY<chart.lastY);
});

test('LIVE chart never connects different wallet coverage or observation gaps',()=>{
  const p=(at,value,coverage='XRP:full')=>({at,value,coverage});
  const chart=livePortfolioSparkline([p(0,12),p(30000,12.01),p(60000,7,'XRP:unknown'),
    p(90000,12),p(120000,12.01),p(300000,12.02)]);
  assert.equal(chart.count,5);
  assert.equal((chart.line.match(/M/g)||[]).length,3);
  assert.equal((chart.line.match(/L/g)||[]).length,2);
  assert.equal(livePortfolioSparkline([p(0,12),p(30000,7,'XRP:unknown')]).count,1);
});
