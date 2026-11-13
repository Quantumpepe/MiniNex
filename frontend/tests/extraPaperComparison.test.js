import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {EXTRA_REPLAY_COINS} from '../src/extraReplayRules.js';
import {validExtraPaperComparison} from '../src/extraPaperComparisonRules.js';

const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const ui=read('../src/ExtraPaperComparison.jsx');
const setup=read('../src/TradingSetupPreview.jsx');
const wallet=read('../src/PrivyWalletPanel.jsx');
const translations=read('../src/i18n.js');
const css=read('../src/styles.css');

const defaultRow=asset=>({
  asset,status:'NOT_STARTED',paper_only:true,simulation_only:true,
  real_trading_enabled:false,wallet_funds_used:false,budget_isolated:true,
  parameters_unchanged:true,recent_window_limit:30,
  cost_model:'GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES',
  budget_usd:null,estimated_equity_usd:null,estimated_total_pnl_usd:null,
  estimated_net_return_pct:null,quote_valid:false,
  cumulative_realized_pnl_usd:null,trade_count:0,recent_buys:0,
  recent_closed_sells:0,recent_winning_sells:0,recent_losing_sells:0,
  recent_flat_sells:0,recent_realized_pnl_usd:null,
  recent_estimated_costs_usd:null,recent_costs_complete:false,
  runtime_active_seconds:null,pause_exit_pending:false,
  last_decision:null,last_reason:null,
});
const rows=EXTRA_REPLAY_COINS.map(defaultRow);
const base={
  paper_only:true,simulation_only:true,real_trading_enabled:false,
  wallet_funds_used:false,parameters_unchanged:true,recent_window_limit:30,
  assets:rows,
};

test('comparison is exactly the nine approved coins, not BTC, ETH or ADA',()=>{
  assert.equal(rows.length,9);
  assert.deepEqual(rows.map(r=>r.asset),[
    'BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI']);
  assert.equal(validExtraPaperComparison(base),true);
  assert.equal(validExtraPaperComparison({...base,assets:rows.slice(0,8)}),false);
  assert.equal(validExtraPaperComparison({
    ...base,assets:[...rows.slice(0,8),defaultRow('ADA')],
  }),false);
  assert.equal(validExtraPaperComparison({
    ...base,assets:[...rows.slice(1),rows[0]],
  }),false);
});

test('contract permits legitimate marked equity but refuses unverified net returns',()=>{
  const entry={...defaultRow('SOL'),status:'ACTIVE',budget_usd:500,
    quote_valid:true,estimated_equity_usd:476.33,
    estimated_total_pnl_usd:-23.67,estimated_net_return_pct:-4.734,
    cumulative_realized_pnl_usd:1.5,trade_count:10,
    recent_buys:6,recent_closed_sells:4,
    recent_winning_sells:1,recent_losing_sells:3,
    recent_flat_sells:0,recent_realized_pnl_usd:-17.5,
    recent_estimated_costs_usd:8.9,recent_costs_complete:true,
  };
  const updated={...base,assets:rows.map(row=>row.asset==='SOL'?entry:row)};
  assert.equal(validExtraPaperComparison(updated),true);
  for(const patch of [
    {wallet_funds_used:true},{real_trading_enabled:true},
    {budget_isolated:false},{parameters_unchanged:false},
    {cost_model:'LIVE_FEES'},{quote_valid:false},{estimated_equity_usd:null},
    {estimated_net_return_pct:NaN},{trade_count:-1},
    {recent_buys:0.3},{recent_costs_complete:'true'},
    {budget_usd:5001},
  ]){
    const bad={...updated,assets:updated.assets.map(x=>x.asset==='SOL'?{...entry,...patch}:x)};
    assert.equal(validExtraPaperComparison(bad),false,JSON.stringify(patch));
  }
  const stale={...entry,quote_valid:false,estimated_equity_usd:null,
    estimated_total_pnl_usd:null,estimated_net_return_pct:null};
  assert.equal(validExtraPaperComparison({...updated,
    assets:updated.assets.map(x=>x.asset==='SOL'?stale:x)}),true);
  assert.equal(validExtraPaperComparison({...updated,
    assets:updated.assets.map(x=>x.asset==='SOL'?{
      ...stale,estimated_total_pnl_usd:-2,
    }:x)}),false);
});

test('UI requires one user tap, one private GET, resets on user changes',()=>{
  assert.match(ui,/onClick=\{toggle\}/);
  assert.match(ui,/API_BASE\+'\/api\/extra-paper\/comparison'/);
  assert.match(ui,/method:'GET'/);
  assert.match(ui,/cache:'no-store'/);
  assert.match(ui,/Authorization:'Bearer '\+token/);
  assert.match(ui,/validExtraPaperComparison\(result\)/);
  assert.match(ui,/new AbortController\(\)/);
  assert.match(ui,/setTimeout\(\(\)=>controller\.abort\(\),12000\)/);
  assert.match(ui,/\[enabled,getAccessToken,userId\]/);
  assert.match(setup,/<ExtraPaperComparison t=\{t\} enabled=\{enabled\}/);
  assert.match(setup,/userId=\{userId\}/);
  assert.match(wallet,/userId=\{user\?\.id\}/);
  assert.doesNotMatch(ui,/setInterval\s*\(/);
  assert.doesNotMatch(ui,/method:'POST'|method:'PUT'|method:'DELETE'/);
  assert.doesNotMatch(ui,/eth_sendTransaction|sendTransaction|signTransaction|privateKey|delegateWallet|approve\(/);
});

test('clear German and English short-window caution and compact mobile style',()=>{
  for(const phrase of [
    "extraCompareTitle: 'Compare PAPER coins'",
    "extraCompareTitle: 'PAPER-Coins vergleichen'",
    'extraCompareDisclaimer:',
    'extraCompareNoQuote:',
    'extraCompareCosts:',
    'extraCompareSellWins:',
  ])assert.equal(translations.includes(phrase),true,phrase);
  assert.match(css,/\.mininex-paper-compare-list\{/);
  assert.match(css,/max-height:310px/);
  assert.match(css,/scrollbar-width:none/);
  assert.match(ui,/row\.recent_winning_sells/);
  assert.match(ui,/row\.recent_closed_sells/);
  assert.match(ui,/row\.recent_costs_complete/);
  assert.match(ui,/row\.quote_valid/);
});
