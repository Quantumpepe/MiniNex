import test from 'node:test';
import assert from 'node:assert/strict';

import {readFileSync} from 'node:fs';
const control=readFileSync(new URL('../src/ExtraCoinPaperControl.jsx',import.meta.url),'utf8');

test('BTC and ETH started paper budgets display as numbers, not unformatted strings',()=>{
  assert.match(control,/const initial=Number\(result\.demo_start_cash\?\?result\.position\?\.demo_start_cash\)/);
  assert.match(control,/result\.budget_usd=Number\.isFinite\(initial\)&&initial>0\?initial:null/);
  assert.match(control,/formatMoney\(data\.budget_usd,document\.documentElement\.lang\)/);
});

import {liveOwnerSessionPath,validLiveOwnerSession,liveOwnerCounters}
  from '../src/extraPaperRules.js';

test('XRP BTC SOL TRX use native LIVE status; EVM coins stay on EVM API',()=>{
  for(const coin of ['BTC','SOL','XRP','TRX'])
    assert.equal(liveOwnerSessionPath(coin),
      '/api/live/native/auto-session/'+coin);
  for(const coin of ['ETH','BNB','POL','AVAX','LINK','QNT','UNI','ARB'])
    assert.equal(liveOwnerSessionPath(coin),
      '/api/live/evm/auto-session/'+coin);
  assert.equal(liveOwnerSessionPath('DOGE'),null);
  const native={mode:'OWNER_NATIVE_LIVE_SESSION_CONTROL',
    asset:'XRP',status:'ACTIVE',worker_attached:true,
    original_blockchain_sources_sent:2,original_native_payouts_finalized:1};
  assert.equal(validLiveOwnerSession(native,'XRP'),true);
  assert.deepEqual(liveOwnerCounters(native,'XRP'),{sent:2,finalized:1});
  assert.equal(validLiveOwnerSession(native,'ETH'),false);
  assert.equal(validLiveOwnerSession({...native,asset:'ETH'},'ETH'),false);
  assert.match(control,/API_BASE\+\(liveMode\?livePath:paperPath\+asset\)/);
  assert.match(control,/!liveMode&&\['ACTIVE','PAUSED'\]/);
  assert.match(control,/fetchOne\(\)/);
});

test('real LIVE controls never start paper sessions or treat PAPER as receipts',()=>{
  assert.doesNotMatch(control,/liveMode&&action==='start'/);
  assert.match(control,/if\(liveMode&&!\['pause','exit'\]\.includes\(action\)\)return/);
  assert.match(control,/if\(action==='exit'&&\(!liveMode\|\|!\['BTC','SOL','XRP','TRX'\]\.includes\(asset\)/);
  assert.match(control,/liveOwnerSessionPath\(asset\)\+'\/'\+action/);
  assert.match(control,/action==='exit'\?'EXIT_PENDING':'PAUSED'/);
  assert.match(control,/latest\.status!==expected&&\!\(action==='exit'&&latest\.status==='STOPPED'\)/);
});
