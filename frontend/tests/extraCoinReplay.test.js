import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {EXTRA_REPLAY_COINS,defaultReplayOption,validExtraReplayBudget,
  validExtraReplayResult} from '../src/extraReplayRules.js';

const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const ui=read('../src/ExtraCoinStrategyReplay.jsx');
const setup=read('../src/TradingSetupPreview.jsx');
const coinGrid=read('../src/CoinGrid.jsx');
const list=read('../src/coins.js');
const labels=read('../src/i18n.js');
const css=read('../src/styles.css');

const option={mode:'LONG',horizon:'15m',budget:'500'};
const sample={
  asset:'SOL',mode:'LONG',horizon:'15m',status:'SIMULATED',
  paper_only:true,hypothetical:true,simulation_only:true,
  real_trading_enabled:false,wallet_funds_used:false,
  orders_executed:0,budget_isolated:true,
  historical_quotes_not_independently_verified:true,
  cost_model:'GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES',
  sample_count:90,initial_usd:500,observed_minutes:90,
  estimated_net_return_pct:-3.52,
  estimated_buy_and_hold_return_pct:-5.21,
  estimated_strategy_minus_hold_pp:1.69,
  estimated_max_drawdown_pct:7.29,
  estimated_final_liquidation_usd:482.40,
  trade_count:1,trades:[{kind:'PAPER_BUY',asset:'SOL',real_trade:false,
                           at:'2026-11-12T10:00:00Z',total_spent:100}],
};

test('the nine extra coins share the strategy; BTC ETH and ADA stay untouched',()=>{
  assert.deepEqual(EXTRA_REPLAY_COINS,
    ['BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI']);
  assert.equal(EXTRA_REPLAY_COINS.includes('ADA'),false);
  assert.equal(EXTRA_REPLAY_COINS.includes('ETH'),false);
  assert.equal(EXTRA_REPLAY_COINS.includes('BTC'),false);
  assert.equal(defaultReplayOption().budget,'500');
  assert.match(setup,/const selectedSettings=opened==='BTC'\|\|opened==='ETH'/);
  assert.match(setup,/selectedCoin\.inProgress \?/);
  assert.match(setup,/<ExtraCoinStrategyReplay asset=\{opened\}/);
  assert.match(setup,/EXTRA_REPLAY_COINS\.map\(coin=>\[coin,defaultReplayOption\(\)\]\)/);
  assert.match(setup,/setReplayOptions\(old=>/);
  assert.match(coinGrid,/COINS\.map\(coin=>/);
  assert.match(list,/symbol:'ADA'.*inProgress:true/);
  assert.match(list,/symbol:'ETH'.*paperSettingsReady:true/);
  assert.match(list,/symbol:'BTC'.*paperSettingsReady:true/);
});

test('each optional virtual budget is independent and capped at the demo limit',()=>{
  for(const valid of ['100','500','1250','5000'])
    assert.equal(validExtraReplayBudget(valid),true,valid);
  for(const bad of ['0','99','5001','5500','0500','5e2','500.5','-10','NaN',
                    '',null,undefined,500,'1,000','100000'])
    assert.equal(validExtraReplayBudget(bad),false,String(bad));
  const a=defaultReplayOption(),b=defaultReplayOption();
  a.budget='900';b.budget='100';
  assert.notEqual(a.budget,b.budget);
});

test('historical response must show no live executions or wallet funds',()=>{
  assert.equal(validExtraReplayResult(sample,'SOL',option),true);
  for(const patch of [
    {paper_only:false},{hypothetical:false},{real_trading_enabled:true},
    {wallet_funds_used:true},{orders_executed:1},{budget_isolated:false},
    {simulation_only:false},{historical_quotes_not_independently_verified:false},
    {cost_model:'LIVE_SWAP_QUOTE'},{status:'LIVE'},
    {asset:'ETH'},{mode:'SPOT_SHORT'},{horizon:'1h'},
    {sample_count:4000},{trade_count:-1},
    {initial_usd:600},{estimated_net_return_pct:Infinity},
    {estimated_max_drawdown_pct:NaN},{estimated_buy_and_hold_return_pct:null},
    {trades:[{...sample.trades[0],real_trade:true}]},
    {trades:[{...sample.trades[0],asset:'ADA'}]},
    {trades:[{...sample.trades[0],kind:'SWAP'}]},
  ]){
    assert.equal(validExtraReplayResult({...sample,...patch},'SOL',option),false,
      JSON.stringify(patch));
  }
  assert.equal(validExtraReplayResult(sample,'SOL',null),false);
  assert.equal(validExtraReplayResult(null,'SOL',option),false);
  assert.equal(validExtraReplayResult(sample,'ADA',option),false);
});

test('not enough recent candles never masquerades as successful simulation',()=>{
  for(const status of ['INSUFFICIENT_WINDOW','INSUFFICIENT_SAMPLES']){
    const response={asset:'XRP',mode:'LONG',horizon:'24h',
      status,paper_only:true,hypothetical:true,
      real_trading_enabled:false,wallet_funds_used:false,
      budget_isolated:true,orders_executed:0,
      sample_count:90,trades:[]};
    assert.equal(validExtraReplayResult(response,'XRP',
      {mode:'LONG',horizon:'24h',budget:'500'}),true);
    assert.equal(validExtraReplayResult({...response,trades:sample.trades},
      'XRP',{mode:'LONG',horizon:'24h',budget:'500'}),false);
    assert.equal(validExtraReplayResult({...response,orders_executed:1},
      'XRP',{mode:'LONG',horizon:'24h',budget:'500'}),false);
  }
});

test('UI runs only when user taps once; no background refresh, API credentials or trade calls',()=>{
  assert.match(ui,/onSubmit=\{run\}/);
  assert.match(ui,/fetch\(API_BASE\+'\/api\/strategy\/replay\/'\+asset/);
  assert.match(ui,/new URLSearchParams\(/);
  assert.match(ui,/validExtraReplayResult\(data,asset,options\)/);
  assert.match(ui,/const controller=new AbortController\(\)/);
  assert.match(ui,/signal:controller\.signal/);
  assert.match(ui,/setTimeout\(\(\)=>controller\.abort\(\),15000\)/);
  assert.doesNotMatch(ui,/setInterval\s*\(/);
  assert.doesNotMatch(ui,/sendTransaction|signTransaction|delegateWallet|privateKey|approve\(/);
  assert.doesNotMatch(ui,/\/api\/user-paper\//);
  assert.doesNotMatch(ui,/POST|PUT|DELETE/);
  assert.match(labels,/extraReplayRun: 'Strategie testen'/);
  assert.match(labels,/extraReplayRun: 'Test strategy'/);
  assert.match(labels,/extraReplayCostWarning:/);
  assert.match(css,/\.mininex-extra-replay-result\{/);
  assert.match(css,/\.mininex-extra-replay-trades\{/);
});
