import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const compact=read('../src/CompactOverview.jsx');
const app=read('../src/App.jsx');
const css=read('../src/styles.css');

test('compact Home does not discard the fifth or subsequent account PAPER trade',()=>{
  assert.match(compact,/const recentTrades=Array\.isArray\(trades\)\?\[\.\.\.trades\]\.reverse\(\):\[\]/);
  assert.match(compact,/const hasMoreTrades=recentTrades\.length>4/);
  assert.match(compact,/\{recentTrades\.map\(\(trade,index\)=>\{/);
  assert.doesNotMatch(compact,/reverse\(\)\.slice\(0,4\)/);
  assert.match(compact,/hasMoreTrades\?' compact-trade-list--scroll':''/);
  assert.match(compact,/tabIndex=\{hasMoreTrades\?0:undefined\}/);
  assert.match(compact,/role=\{hasMoreTrades\?'region':undefined\}/);
  assert.match(compact,/aria-label=\{hasMoreTrades\?t\.recentTrades\+' · '\+asset:undefined\}/);
});

test('desktop Home uses the same four-visible-row scrolling and activity keeps its full list',()=>{
  assert.match(app,/const shown=\(Array\.isArray\(trades\)\?\[\.\.\.trades\]:\[\]\)\.reverse\(\)\.slice\(0,40\)/);
  assert.match(app,/const hasMoreTrades=compact&&shown\.length>4/);
  assert.match(app,/hasMoreTrades\?' trade-list--scroll':''/);
  assert.match(app,/<ActivityList trades=\{personalTrades\} t=\{t\} lang=\{language\} asset=\{asset\} compact\/>/);
  assert.match(app,/function usePersonalPaperTrades\(asset, paperVisible, refreshNonce\)/);
  assert.match(app,/trade\?\.real_trade===false/);
  assert.match(app,/data\?\.asset!==asset/);
  assert.match(app,/data\?\.paper_only!==true/);
});

test('four visible rows; scroll inside the card from fifth; scrollbar stays invisible',()=>{
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
