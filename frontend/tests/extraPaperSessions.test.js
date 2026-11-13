import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {EXTRA_REPLAY_COINS,defaultReplayOption} from '../src/extraReplayRules.js';
import {validExtraPaperState,extraPaperCanStart} from '../src/extraPaperRules.js';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const control=read('../src/ExtraCoinPaperControl.jsx');
const replay=read('../src/ExtraCoinStrategyReplay.jsx');
const setup=read('../src/TradingSetupPreview.jsx');
const labels=read('../src/i18n.js');
const css=read('../src/styles.css');
const fixture={
  asset:'SOL',status:'ACTIVE',paper_only:true,simulation_only:true,
  real_trading_enabled:false,wallet_funds_used:false,budget_isolated:true,
  cost_model:'GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES',
  budget_usd:500,settlement:'USDC',strategy_mode:'LONG',
  horizon:'15m',revision:2,pause_exit_pending:false,
  position:{stable_cash:'350.5',coin_quantity:'0.4',
    cost_basis:'149.5',realized_net_profit:'-2.25',trade_count:1},
  trades:[{asset:'SOL',kind:'PAPER_BUY',real_trade:false,
           at:'2026-11-13T10:00:00Z',total_spent:149.5}],
};

test('only nine extra coins may run server PAPER sessions, ADA remains pending',()=>{
  assert.deepEqual(EXTRA_REPLAY_COINS,[
    'BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI',
  ]);
  assert.equal(EXTRA_REPLAY_COINS.includes('ADA'),false);
  assert.equal(EXTRA_REPLAY_COINS.includes('BTC'),false);
  assert.equal(EXTRA_REPLAY_COINS.includes('ETH'),false);
  assert.match(setup,/const selectedSettings=opened==='BTC'\|\|opened==='ETH'/);
  assert.match(setup,/selectedCoin\.inProgress\?t\.coinInProgress/);
  assert.match(replay,/<ExtraCoinPaperControl asset=\{asset\}/);
  assert.match(setup,/getAccessToken=\{getAccessToken\}/);
  assert.match(labels,/coinInProgress: 'In Arbeit'/);
});

test('individual virtual budgets and USD stable settlement validate without authorizing signing',()=>{
  assert.deepEqual(defaultReplayOption(),{
    mode:'LONG',horizon:'15m',budget:'500',stable:'USDC',
  });
  for(const symbol of EXTRA_REPLAY_COINS){
    const settings={...defaultReplayOption()};
    settings.budget=symbol==='SOL'?'250':'900';
    assert.equal(extraPaperCanStart(settings),true);
  }
  assert.equal(extraPaperCanStart({...defaultReplayOption(),budget:'5001'}),false);
  assert.equal(extraPaperCanStart({...defaultReplayOption(),stable:'BTC'}),false);
  assert.equal(extraPaperCanStart({...defaultReplayOption(),mode:'LEVERAGED_SHORT'}),false);
  assert.equal(extraPaperCanStart({...defaultReplayOption(),horizon:'36h'}),false);
  assert.equal(extraPaperCanStart(null),false);
  assert.match(setup,/\['mode','horizon','budget','stable'\]/);
});

test('server active and paused paper records must be wallet-free, account-specific',()=>{
  assert.equal(validExtraPaperState(fixture,'SOL'),true);
  assert.equal(validExtraPaperState({...fixture,status:'PAUSED'},'SOL'),true);
  assert.equal(validExtraPaperState({
    asset:'BNB',status:'NOT_STARTED',paper_only:true,simulation_only:true,
    real_trading_enabled:false,wallet_funds_used:false,budget_isolated:true,
    cost_model:'GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES',trades:[],
  },'BNB'),true);
  for(const patch of [
    {asset:'XRP'},{status:'LIVE'},{paper_only:false},{simulation_only:false},
    {real_trading_enabled:true},{wallet_funds_used:true},{budget_isolated:false},
    {cost_model:'LIVE_SWAP'}, {budget_usd:5001}, {budget_usd:0},
    {settlement:'ETH'}, {strategy_mode:'MARGIN'}, {horizon:'36h'},
    {pause_exit_pending:null},{revision:-1},
    {position:{...fixture.position,stable_cash:'NaN'}},
    {position:{...fixture.position,coin_quantity:'-1'}},
    {position:{...fixture.position,trade_count:0}},
    {trades:[{...fixture.trades[0],asset:'UNI'}]},
    {trades:[{...fixture.trades[0],real_trade:true}]},
    {trades:[{...fixture.trades[0],kind:'LIVE_BUY'}]},
  ])assert.equal(validExtraPaperState({...fixture,...patch},'SOL'),false,
    JSON.stringify(patch));
  assert.equal(validExtraPaperState(fixture,'ADA'),false);
  assert.equal(validExtraPaperState(null,'SOL'),false);
});

test('ongoing PAPER lives on server with 20-second focused refresh and no market polling',()=>{
  assert.match(control,/API_BASE\+'\/api\/extra-paper\/'\+asset/);
  assert.match(control,/getAccessToken\(\)/);
  assert.match(control,/Authorization:'Bearer '\+token/);
  assert.match(control,/method:'GET'/);
  assert.match(control,/method:'POST'/);
  assert.match(control,/JSON\.stringify\(payload\)/);
  assert.match(control,/busyRef\.current/);
  assert.match(control,/data\.status==='ACTIVE'\|\|data\.pause_exit_pending/);
  assert.match(control,/setInterval\(poll,20000\)/);
  assert.match(control,/document\.hidden/);
  assert.match(control,/action==='pause'/);
  assert.match(control,/action==='resume'/);
  assert.match(control,/action==='start'/);
  assert.match(control,/budget_usd:Number\(options\.budget\)/);
  assert.match(control,/validExtraPaperState\(result,asset\)/);
  assert.match(control,/\.slice\(-4\)\.reverse\(\)/);
  assert.doesNotMatch(control,/\/api\/market\//);
  assert.doesNotMatch(control,/eth_sendTransaction|sendTransaction|signTransaction|privateKey|delegateWallet|wallet\.send|approve\(/);
});

test('simple DE/EN labels, mobile layout, no new home controls or ADA changes',()=>{
  for(const phrase of [
    "extraPaperTitle: 'Automatic simulation · PAPER'",
    "extraPaperTitle: 'Automatische Simulation · PAPER'",
    "extraPaperStart: 'Paper-Handel starten'",
    "extraPaperPause: 'Pausieren und umwandeln in'",
    "extraPaperOnly: 'NUR SIMULATION",
    "extraPaperPending:",
    "extraPaperRuntime:",
  ])assert.equal(labels.includes(phrase),true,phrase);
  assert.match(css,/\.mininex-extra-paper-balances\{/);
  assert.match(css,/\.mininex-extra-paper-history\{/);
  assert.match(css,/scrollbar-width:none/);
  assert.match(setup,/renderSessionControls\?\.\(opened\)/);
  assert.match(replay,/onSubmit=\{run\}/);
});
