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
  assert.equal(summarizeHomePaperPortfolio(c,e,'ARB').equity,545);
});

test('ALL sums all twelve independent started sessions regardless of selected coin',()=>{
  const c={BTC:core('BTC',520,500),ETH:core('ETH',460,450)};
  const e=report();
  const all=summarizeHomePaperPortfolio(c,e,'ALL');
  assert.equal(all.assetCount,12);
  assert.equal(all.startingCapital,950+10*500);
  assert.equal(all.equity,520+460+10*545);
  assert.equal(all.pnl,all.equity-all.startingCapital);
  assert.equal(all.scope,'ALL');
  assert.equal(all.mode,'PAPER');
  assert.equal(all.runningAssets,12);
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
  assert.equal(total.assetCount,10);
  assert.equal(total.startingCapital,500+9*500);
  assert.equal(total.equity,480+9*545);
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
  assert.equal(all.startingCapital,6000);
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
  assert.doesNotMatch(app,/usePaperPortfolioHome\(paperVisible,refreshNonce,homePaperScope/);
  assert.match(app,/const homePaperScope=showAllPaperHistory\?'ALL':asset/);
  assert.match(mobile,/scope="ALL" goTo=\{goTo\}/);
  assert.match(app,/useAllPaperHomeTrades\(paperVisible,page==='overview',refreshNonce,homePaperScope\)/);
  assert.match(app,/setShowAllPaperHistory\(false\);setAsset\(symbol\)/);
  assert.match(app,/const \[showAllPaperHistory,setShowAllPaperHistory\]=useState\(false\)/);
  assert.match(mobile,/scope="ALL" goTo=\{goTo\}/);
  assert.match(mobile,/homePortfolioCapital/);
  assert.match(mobile,/summary\?\.valuationAvailable/);
  assert.match(labels,/homePortfolioCapital: 'Virtuelles Startkapital'/);
  assert.match(labels,/homePortfolioCapital: 'Virtual starting capital'/);
});

test('independent PAPER cutover leaves LIVE wallet tools and coin settings visible',()=>{
  assert.match(app,/getJson\('\/api\/user-paper\/availability'\)/);
  assert.match(app,/window\.setInterval\(\(\)=>\{/);
  assert.match(app,/setPaperVisible\(\s*data\?\.paper_available===true/);
  assert.match(mobile,/\{showSimulatedTrades&&<PaperPortfolioCard/);
  assert.match(mobile,/\{showSimulatedTrades&&<section className="compact-recent-trades"/);
  assert.match(app,/\{paperVisible&&<section className="panel recent-panel"/);
  assert.match(app,/\{paperVisible&&<UserPaperSessions/);
  assert.match(wallet,/paperAvailable=\{paperAvailable\}/);
  assert.match(settings,/\{paperAvailable&&<ExtraPaperComparison/);
  assert.match(settings,/\{selectedSettings \? <>/);
  assert.match(settings,/\{paperAvailable&&renderSessionControls\?\.\(opened\)\}/);
  assert.match(wallet,/<WalletSend/);
  assert.match(wallet,/<ManualSwapPreview/);
  assert.match(wallet,/<WalletBalances/);
  assert.doesNotMatch(mobile,/LIVE —/);
});


test('backend ten-extra-coin response including ARB restores 12-coin total without selection filter',()=>{
  assert.deepEqual(HOME_PAPER_PORTFOLIO_COINS,[
    'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ARB',
  ]);
  const e=report();
  assert.equal(e.assets.length,10);
  const result=summarizeHomePaperPortfolio({
    BTC:core('BTC',520,500),ETH:core('ETH',515,500),
  },e,'ALL');
  assert.equal(result.assetCount,12);
  assert.equal(result.equity,520+515+10*545);
  assert.equal(result.startingCapital,6000);
  assert.equal(result.valuationAvailable,true);
  assert.equal(result.pnl,result.equity-6000);
  const noArb={...e,assets:e.assets.filter(row=>row.asset!=='ARB')};
  assert.equal(summarizeHomePaperPortfolio({},noArb,'ALL'),null);
  assert.equal(summarizeHomePaperPortfolio({},e,'ARB').equity,545);
});

test('owner-defined budgets above 5000 remain visible in aggregated capital',()=>{
  const e=report();
  e.assets=e.assets.map(row=>row.asset==='ARB'?extra('ARB',10123,10000):row);
  const result=summarizeHomePaperPortfolio({
    BTC:{status:'NOT_STARTED',paper_only:true,real_trading_enabled:false,wallet_funds_used:false},
    ETH:{status:'NOT_STARTED',paper_only:true,real_trading_enabled:false,wallet_funds_used:false},
  },e,'ALL');
  assert.equal(result.assetCount,10);
  assert.equal(result.startingCapital,9*500+10000);
  assert.equal(result.equity,9*545+10123);
  assert.equal(result.valuationAvailable,true);
});
