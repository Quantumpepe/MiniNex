import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const compact=read('../src/CompactOverview.jsx');
const app=read('../src/App.jsx');
const css=read('../src/styles.css');

test('compact home keeps the fifth and later account PAPER trade, newest first',()=>{
  assert.match(compact,/const recentTrades=Array\.isArray\(trades\)\?trades\.slice\(0,60\):\[\]/);
  assert.match(compact,/const hasMoreTrades=recentTrades\.length>4/);
  assert.match(compact,/\{recentTrades\.map\(\(trade,index\)=>\{/);
  assert.doesNotMatch(compact,/reverse\(\)\.slice\(0,4\)/);
  assert.match(compact,/hasMoreTrades\?' compact-trade-list--scroll':''/);
  assert.match(compact,/tabIndex=\{hasMoreTrades\?0:undefined\}/);
  assert.match(compact,/role=\{hasMoreTrades\?'region':undefined\}/);
  assert.match(compact,/aria-label=\{hasMoreTrades\?t\.recentTrades\+' · '\+t\.homeAllCoins:undefined\}/);
});

test('desktop home reads all coins from the same private paper feed',()=>{
  assert.match(app,/const shown=Array\.isArray\(trades\)\?trades\.slice\(0,60\):\[\]/);
  assert.match(app,/const hasMoreTrades=compact&&shown\.length>4/);
  assert.match(app,/hasMoreTrades\?' trade-list--scroll':''/);
  assert.match(app,/<ActivityList trades=\{homePaperTrades\.trades\} status=\{homePaperTrades\.status\} t=\{t\} lang=\{language\} compact\/>/);
  assert.match(app,/useAllPaperHomeTrades\(paperVisible,page==='overview',refreshNonce\)/);
  assert.match(app,/<h2>\{t\.recentTrades\} · \{t\.homeAllCoins\}<\/h2>/);
});

test('four rows visible; fifth scrolls inside card without any scrollbar',()=>{
  assert.match(css,/\.compact-trade-row\{[\s\S]*?height:66px;[\s\S]*?flex:0 0 66px;/);
  assert.match(css,/\.compact-trade-list--scroll\{\s*max-height:264px;/);
  assert.match(css,/\.recent-panel \.trade-row\{[\s\S]*?height:68px;/);
  assert.match(css,/\.recent-panel \.trade-list--scroll\{\s*max-height:272px;/);
  assert.match(css,/\.compact-trade-list--scroll,\s*\.recent-panel \.trade-list--scroll\{[\s\S]*?overflow-y:auto;/);
  assert.match(css,/overscroll-behavior-y:contain/);
  assert.match(css,/-webkit-overflow-scrolling:touch/);
  assert.match(css,/touch-action:pan-y/);
  assert.match(css,/scrollbar-width:none/);
  assert.match(css,/\.compact-trade-list--scroll::-webkit-scrollbar,[\s\S]*?display:none/);
  assert.match(css,/\.compact-trade-list--scroll:focus-visible/);
  assert.match(css,/\.recent-panel \.trade-list--scroll:focus-visible/);
});
