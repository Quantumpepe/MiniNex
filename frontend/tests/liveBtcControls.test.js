import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {validLiveBtcLimits,validatedLiveBtcDraft}
  from '../src/liveBtcRules.js';

const read=file=>readFileSync(new URL(file,import.meta.url),'utf8');

const limits=()=>({
  asset:'BTC',network:'bitcoin-mainnet',
  mode:'BTC_LIVE_LIMITS_PREPARATION_ONLY',
  planned_capital_usd:'9900',max_order_usd:'1000',
  max_daily_turnover_usd:'7000',
  revision:4,trading_paused:true,
  privy_btc_signer_verified:false,
  btc_crosschain_execution_available:false,
  native_utxo_spend_authorized:false,
  paper_budget_used:false,live_trading_enabled:false,
  automated_signing_enabled:false,
});

test('BTC live budget allows more than 5000 without changing paper limits',()=>{
  assert.equal(validLiveBtcLimits(limits()),true);
  const draft={
    planned_capital_usd:'9900',max_order_usd:'1000',
    max_daily_turnover_usd:'7000',
  };
  assert.deepEqual(validatedLiveBtcDraft(draft),draft);
  assert.equal(validatedLiveBtcDraft({...draft,max_order_usd:'7001'}),null);
  assert.equal(validatedLiveBtcDraft({...draft,planned_capital_usd:'NaN'}),null);
});

test('BTC risk response must be independent and never claim unlocked signer',()=>{
  for(const mutate of [
    a=>{a.asset='ETH';},a=>{a.network='ethereum-mainnet';},
    a=>{a.trading_paused=false;},a=>{a.live_trading_enabled=true;},
    a=>{a.automated_signing_enabled=true;},
    a=>{a.native_utxo_spend_authorized=true;},
    a=>{a.btc_crosschain_execution_available=true;},
    a=>{a.paper_budget_used=true;},
    a=>{a.privy_btc_signer_verified=true;},
    a=>{a.revision=-1;},
    a=>{a.max_order_usd='8000';},
  ]){
    const copy=limits();mutate(copy);
    assert.equal(validLiveBtcLimits(copy),false);
  }
});

test('BTC live limits appear only under selected BTC coin, never global',()=>{
  const setup=read('../src/TradingSetupPreview.jsx');
  const btc=read('../src/LiveBtcControls.jsx');
  const eth=read('../src/LiveEthControls.jsx');
  const panel=read('../src/PrivyWalletPanel.jsx');
  const labels=read('../src/i18n.js');
  assert.match(setup,/opened==='BTC'&&<LiveBtcControls/);
  assert.match(setup,/userId=\{userId\} getAccessToken=\{getAccessToken\}/);
  assert.match(setup,/renderSessionControls\?\.\(opened\)/);
  assert.match(panel,/<LiveEthControls/);
  assert.doesNotMatch(btc,/sendTransaction\(|eth_sendTransaction|addSigners\(|sendBitcoin/);
  assert.match(btc,/\/api\/live\/btc\/limits/);
  assert.match(btc,/\/api\/live\/btc\/stop/);
  assert.match(btc,/expected_revision:limits.revision/);
  assert.match(btc,/validLiveBtcLimits\(data\)/);
  assert.match(eth,/\/api\/live\/eth\/limits/);
  assert.match(labels,/liveBtcTitle:/);
  assert.match(labels,/liveBtcStop:/);
  assert.match(labels,/liveBtcPaused:/);
});
