import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {HOME_PAPER_COINS,validHomePaperFeed} from '../src/homePaperFeed.js';
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const ui=read('../src/CompactOverview.jsx');
const app=read('../src/App.jsx');
const hook=read('../src/useAllPaperHomeTrades.js');
const css=read('../src/styles.css');
const messages=read('../src/i18n.js');

const a=(asset,id,at,kind='PAPER_BUY')=>({
  id:asset+':'+id,asset,kind,at,settlement:'USDC',
  price_reference:105.32,quantity:0.5,amount_usd:50.2,
  realized_net_profit:kind==='PAPER_SELL'?-3.14:null,
  reason:'REBOUND',paper_only:true,real_trade:false,
});
const base={
  paper_only:true,real_trading_enabled:false,wallet_funds_used:false,
  scope:'PRIVATE_ACCOUNT',sort:'NEWEST_FIRST',selected_asset:'ALL',assets:[...HOME_PAPER_COINS],
  trades:[
    a('SOL',12,'2026-11-13T18:15:00+00:00'),
    a('ETH',8,'2026-11-13T18:12:00+00:00','PAPER_SELL'),
    a('BTC',6,'2026-11-13T18:10:00+00:00'),
    a('XRP',2,'2026-11-13T18:02:00+00:00','PAPER_SELL'),
    a('UNI',3,'2026-11-13T17:45:00+00:00'),
  ],
};

test('all eleven ready paper coins can appear in one home list, not ADA',()=>{
  assert.deepEqual([...HOME_PAPER_COINS],[
    'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI']);
  assert.equal(HOME_PAPER_COINS.includes('ADA'),false);
  assert.equal(validHomePaperFeed(base),true);
  for(const symbol of HOME_PAPER_COINS){
    assert.equal(validHomePaperFeed({...base,trades:[
      a(symbol,1,'2026-11-13T18:15:00Z'),
    ]}),true,symbol);
  }
  assert.equal(validHomePaperFeed({...base,trades:[]}),true);
});

test('only account-owned PAPER fields, strict newest-first timestamps and unique IDs',()=>{
  const invalid=[
    {scope:'PUBLIC'},
    {wallet_funds_used:true},
    {real_trading_enabled:true},
    {paper_only:false},
    {sort:'OLDEST_FIRST'},
    {selected_asset:'ETH'},
    {selected_asset:'SOL'},
    {selected_asset:'ADA'},
    {selected_asset:undefined},
    {assets:[...HOME_PAPER_COINS,'ADA']},
    {assets:[...HOME_PAPER_COINS].reverse()},
    {trades:[a('ADA',1,'2026-11-13T18:15:00Z')]},
    {trades:[a('BTC',1,'2026-11-13T18:15:00Z'),a('BTC',1,'2026-11-13T18:10:00Z')]},
    {trades:base.trades.slice().reverse()},
    {trades:[{...base.trades[0],real_trade:true}]},
    {trades:[{...base.trades[0],paper_only:false}]},
    {trades:[{...base.trades[0],kind:'LIVE_BUY'}]},
    {trades:[{...base.trades[0],id:'ETH:12'}]},
    {trades:[{...base.trades[0],price_reference:Infinity}]},
    {trades:[{...base.trades[0],quantity:0}]},
    {trades:[{...base.trades[0],amount_usd:-1}]},
    {trades:[{...base.trades[0],settlement:'BTC'}]},
    {trades:[{...base.trades[0],realized_net_profit:5.0}]},
    {trades:[{...base.trades[1],realized_net_profit:null}]},
    {trades:[{...base.trades[0],at:'invalid'}]},
    {trades:Array.from({length:61},(_,i)=>a('BTC',i+1,
      '2026-11-13T18:15:00Z'))},
  ];
  for(const patch of invalid){
    assert.equal(validHomePaperFeed({...base,...patch}),false,
      JSON.stringify(patch).slice(0,90));
  }
  assert.equal(validHomePaperFeed(null),false);
  assert.equal(validHomePaperFeed([]),false);
});

test('only one private cached-no store API call on visible overview and user reset',()=>{
  assert.match(hook,/API_BASE\+'\/api\/home\/paper-trades\?asset='/);
  assert.match(hook,/Authorization:'Bearer '\+token/);
  assert.match(hook,/cache:'no-store'/);
  assert.match(hook,/validHomePaperFeed\(data,assetScope\)/);
  assert.match(hook,/const did=user\?\.id\|\|null/);
  assert.match(hook,/\[paperVisible,homeVisible,ready,authenticated,did,getAccessToken,refreshNonce,assetScope\]/);
  assert.match(hook,/new AbortController\(\)/);
  assert.match(hook,/window\.setInterval\(sync,20000\)/);
  assert.match(hook,/document\.hidden/);
  assert.match(hook,/controllers\.clear\(\)/);
  assert.match(hook,/setState\(\{trades:\[\],status:'error'\}\)/);
  assert.doesNotMatch(hook,/sendTransaction|signTransaction|\/api\/live\/orders|method:'POST'/);
});

test('mobile and desktop main page display per-entry asset, amount, price and PnL',()=>{
  assert.match(app,/useAllPaperHomeTrades\(paperVisible,page==='overview',refreshNonce,homePaperScope\)/);
  assert.match(app,/trades=\{homePaperTrades\.trades\}/);
  assert.match(app,/status=\{homePaperTrades\.status\}/);
  assert.match(app,/tradesStatus=\{homePaperTrades\.status\}/);
  assert.match(app,/<h2>\{t\.recentTrades\} · \{showAllPaperHistory\?t\.homeAllCoins:asset\}<\/h2>/);
  assert.match(app,/trade\.asset/);
  assert.match(app,/trade\.amount_usd/);
  assert.match(app,/trade\.price_reference/);
  assert.match(app,/trade\.realized_net_profit/);
  assert.match(ui,/trade\.asset/);
  assert.match(ui,/trade\.amount_usd/);
  assert.match(ui,/trade\.price_reference/);
  assert.match(ui,/trade\.realized_net_profit/);
  assert.match(ui,/\{showSimulatedTrades&&<section className="compact-recent-trades"/);
  assert.doesNotMatch(ui,/showSimulatedTrades&&coinBySymbol\(asset\)\?\.paperSettingsReady&&/);
  assert.match(messages,/homeAllCoins: 'Alle Coins'/);
  assert.match(messages,/homeAllCoins: 'All coins'/);
  assert.match(messages,/homeTradesUnavailable:/);
});

test('only four rows initially, row five onwards scrolls inside field invisibly',()=>{
  assert.match(ui,/const recentTrades=Array\.isArray\(trades\)\?trades\.slice\(0,60\):\[\]/);
  assert.match(ui,/const hasMoreTrades=recentTrades\.length>4/);
  assert.match(ui,/hasMoreTrades\?' compact-trade-list--scroll':''/);
  assert.match(ui,/tabIndex=\{hasMoreTrades\?0:undefined\}/);
  assert.match(ui,/role=\{hasMoreTrades\?'region':undefined\}/);
  assert.match(app,/const hasMoreTrades=compact&&shown\.length>4/);
  assert.match(app,/hasMoreTrades\?' trade-list--scroll':''/);
  assert.match(css,/\.compact-trade-row\{[\s\S]*?height:66px;[\s\S]*?flex:0 0 66px;/);
  assert.match(css,/\.compact-trade-list--scroll\{\s*max-height:264px;/);
  assert.match(css,/\.recent-panel \.trade-row\{[\s\S]*?height:68px;/);
  assert.match(css,/\.recent-panel \.trade-list--scroll\{\s*max-height:272px;/);
  assert.match(css,/\.compact-trade-list--scroll,\s*\.recent-panel \.trade-list--scroll\{[\s\S]*?overflow-y:auto;/);
  assert.match(css,/scrollbar-width:none/);
  assert.match(css,/\.compact-trade-list--scroll::-webkit-scrollbar,[\s\S]*?display:none/);
});


test('default history is selected coin only; ALL is voluntary',()=>{
  const selected={...base,selected_asset:'BTC',trades:[
    a('BTC',7,'2026-11-13T18:15:00+00:00'),
    a('BTC',3,'2026-11-13T18:12:00+00:00'),
  ]};
  assert.equal(validHomePaperFeed(selected,'BTC'),true);
  assert.equal(validHomePaperFeed(selected,'ETH'),false);
  assert.equal(validHomePaperFeed(selected,'ALL'),false);
  assert.equal(validHomePaperFeed({...selected,trades:base.trades},'BTC'),false);
  assert.equal(validHomePaperFeed({...base,selected_asset:'ALL'},'ALL'),true);
  assert.equal(validHomePaperFeed(base,'ADA'),false);
  assert.match(app,/const \[showAllPaperHistory,setShowAllPaperHistory\]=useState\(false\)/);
  assert.match(app,/const homePaperScope=showAllPaperHistory\?'ALL':asset/);
  assert.match(app,/setShowAllPaperHistory\(false\);setAsset\(symbol\)/);
  assert.match(ui,/showAllTrades\?t\.homeAllCoins:asset/);
  assert.match(ui,/onClick=\{onToggleAllTrades\}/);
  assert.match(messages,/homeOnlyCoin: 'Nur dieser Coin'/);
  assert.match(messages,/homeOnlyCoin: 'Only this coin'/);
});
