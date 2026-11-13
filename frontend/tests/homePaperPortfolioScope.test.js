import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {summarizeHomePaperPortfolio,HOME_PAPER_PORTFOLIO_COINS}
  from '../src/paperPortfolio.js';

const src=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const app=src('../src/App.jsx');
const mobile=src('../src/CompactOverview.jsx');
const wallet=src('../src/PrivyWalletPanel.jsx');
const settings=src('../src/TradingSetupPreview.jsx');
const labels=src('../src/i18n.js');

const core=(asset,equity=510,start=500)=>({
  asset,status:'ACTIVE',paper_only:true,real_trading_enabled:false,
  wallet_funds_used:false,demo_start_cash:String(start),
  position:{stable_cash:'260',coin_quantity:'1',valuation:{
    quote_valid:equity!==null,estimated_total_equity_usd:equity,
  }},
});
const extraAssets=HOME_PAPER_PORTFOLIO_COINS.slice(2);
const extra=(asset,equity=545,budget=500)=>({
  asset,status:'ACTIVE',paper_only:true,simulation_only:true,
  real_trading_enabled:false,wallet_funds_used:false,budget_isolated:true,
  budget_usd:budget,estimated_equity_usd:equity,
  quote_valid:equity!==null,
});
const report=()=>({
  paper_only:true,simulation_only:true,real_trading_enabled:false,
  wallet_funds_used:false,assets:extraAssets.map(x=>extra(x)),
});

test('selecting BTC, ETH, SOL or XRP shows only that coin capital by default',()=>{
  const c={BTC:core('BTC',520,500),ETH:core('ETH',460,450)};
  const e=report();
  const btc=summarizeHomePaperPortfolio(c,e,'BTC');
  const eth=summarizeHomePaperPortfolio(c,e,'ETH');
  const sol=summarizeHomePaperPortfolio(c,e,'SOL');
  const xrp=summarizeHomePaperPortfolio(c,e,'XRP');
  assert.equal(btc.equity,520);
  assert.equal(btc.startingCapital,500);
  assert.equal(btc.pnl,20);
  assert.equal(btc.assetCount,1);
  assert.equal(eth.equity,460);
  assert.equal(eth.startingCapital,450);
  assert.equal(eth.assetCount,1);
  assert.equal(sol.equity,545);
  assert.equal(sol.startingCapital,500);
  assert.equal(sol.assetCount,1);
  assert.equal(xrp.equity,545);
  assert.equal(xrp.assetCount,1);
  assert.equal(summarizeHomePaperPortfolio(c,e,'ADA'),null);
});

test('ALL sums independent started sessions but only after an explicit toggle',()=>{
  const c={BTC:core('BTC',520,500),ETH:core('ETH',460,450)};
  const e=report();
  const all=summarizeHomePaperPortfolio(c,e,'ALL');
  assert.equal(all.assetCount,11);
  assert.equal(all.startingCapital,950+9*500);
  assert.equal(all.equity,520+460+9*545);
  assert.equal(all.pnl,all.equity-all.startingCapital);
  assert.equal(all.scope,'ALL');
  assert.equal(all.mode,'PAPER');
  assert.equal(all.runningAssets,11);
  assert.equal(summarizeHomePaperPortfolio(c,null,'BTC').equity,520);
  assert.equal(summarizeHomePaperPortfolio(c,null,'ALL'),null);
});

test('unstarted coin gets NO fabricated capital and does not consume global budget',()=>{
  const c={
    BTC:{status:'NOT_STARTED',paper_only:true,real_trading_enabled:false,
      wallet_funds_used:false},
    ETH:core('ETH',480,500),
  };
  const e=report();
  e.assets=e.assets.map(row=>row.asset==='SOL'?{
    ...row,status:'NOT_STARTED',budget_usd:null,estimated_equity_usd:null,
    quote_valid:false,
  }:row);
  assert.equal(summarizeHomePaperPortfolio(c,e,'BTC'),null);
  assert.equal(summarizeHomePaperPortfolio(c,e,'SOL'),null);
  const total=summarizeHomePaperPortfolio(c,e,'ALL');
  assert.equal(total.assetCount,9);
  assert.equal(total.startingCapital,500+8*500);
  assert.equal(total.equity,480+8*545);
});

test('no verified sell quote means start capital but NO fake equity or PnL',()=>{
  const c={BTC:core('BTC',null,500),ETH:core('ETH',520,500)};
  const e=report();
  const btc=summarizeHomePaperPortfolio(c,e,'BTC');
  assert.equal(btc.startingCapital,500);
  assert.equal(btc.valuationAvailable,false);
  assert.equal(btc.equity,null);
  assert.equal(btc.pnl,null);
  assert.equal(btc.pnlPct,null);
  const all=summarizeHomePaperPortfolio(c,e,'ALL');
  assert.equal(all.startingCapital,5500);
  assert.equal(all.valuationAvailable,false);
  assert.equal(all.equity,null);
  assert.equal(all.pnl,null);
  const bad=report();
  bad.assets[0]={...bad.assets[0],quote_valid:false,estimated_equity_usd:null};
  const bnb=summarizeHomePaperPortfolio({},bad,'BNB');
  assert.equal(bnb.startingCapital,500);
  assert.equal(bnb.equity,null);
});

test('spoofed real accounts and mismatched coin data never become virtual portfolio',()=>{
  const c={BTC:core('BTC')};
  assert.equal(summarizeHomePaperPortfolio({
    BTC:{...c.BTC,wallet_funds_used:true},
  },null,'BTC'),null);
  assert.equal(summarizeHomePaperPortfolio({
    BTC:{...c.BTC,asset:'ETH'},
  },null,'BTC'),null);
  const extraReport=report();
  extraReport.assets[0]={...extraReport.assets[0],real_trading_enabled:true};
  assert.equal(summarizeHomePaperPortfolio({},extraReport,'BNB'),null);
  assert.equal(summarizeHomePaperPortfolio({},extraReport,'ALL'),null);
  const wrong=report();
  wrong.assets.reverse();
  assert.equal(summarizeHomePaperPortfolio({},wrong,'SOL'),null);
});

test('portfolio ALWAYS totals all started coins while ONLY trades follow selected asset',()=>{
  assert.match(app,/const homePaperScope=showAllPaperHistory\?'ALL':asset/);
  assert.match(app,/usePaperPortfolioHome\(paperVisible,refreshNonce,'ALL',page==='overview'\)/);
  assert.match(app,/useAllPaperHomeTrades\(paperVisible,page==='overview',refreshNonce,homePaperScope\)/);
  assert.match(app,/setShowAllPaperHistory\(false\);setAsset\(symbol\)/);
  assert.match(app,/const \[showAllPaperHistory,setShowAllPaperHistory\]=useState\(false\)/);
  assert.match(mobile,/scope="ALL" goTo=\{goTo\}/);
  assert.match(mobile,/homePortfolioCapital/);
  assert.match(mobile,/summary\?\.valuationAvailable/);
  assert.match(labels,/homePortfolioCapital: 'Virtuelles Startkapital'/);
  assert.match(labels,/homePortfolioCapital: 'Virtual starting capital'/);
});

test('live cutover hides all PAPER UI and leaves wallet tools visible',()=>{
  assert.match(app,/getJson\('\/api\/user-paper\/availability'\)/);
  assert.match(app,/window\.setInterval\(\(\)=>\{/);
  assert.match(app,/setPaperVisible\(\s*data\?\.paper_available===true&&data\?\.live_trading_enabled===false/);
  assert.match(mobile,/\{showSimulatedTrades&&<PaperPortfolioCard/);
  assert.match(mobile,/\{showSimulatedTrades&&<section className="compact-recent-trades"/);
  assert.match(app,/\{paperVisible&&<section className="panel recent-panel"/);
  assert.match(app,/\{paperVisible&&<UserPaperSessions/);
  assert.match(wallet,/paperAvailable=\{paperAvailable\}/);
  assert.match(settings,/\{paperAvailable&&<ExtraPaperComparison/);
  assert.match(settings,/\{paperAvailable&&\(selectedSettings \? <>/);
  assert.match(wallet,/<WalletSend/);
  assert.match(wallet,/<ManualSwapPreview/);
  assert.match(wallet,/<WalletBalances/);
  assert.doesNotMatch(mobile,/LIVE —/);
});
