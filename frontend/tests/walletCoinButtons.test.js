import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const setup=read('../src/TradingSetupPreview.jsx');
const wallet=read('../src/PrivyWalletPanel.jsx');
const app=read('../src/App.jsx');
const style=read('../src/styles.css');
const labels=read('../src/i18n.js');
const grid=read('../src/CoinGrid.jsx');
const catalog=read('../src/coins.js');


test('all eight coin buttons have native symbols, verified-only prices and no chevrons',()=>{
  assert.match(catalog,/symbol:'POL',name:'Polygon'/);
  assert.match(setup,/<CoinGrid t=\{t\} lang=\{lang\} selected=\{opened\}/);
  assert.match(grid,/COINS\.map\(coin=>\{/);
  assert.match(grid,/aria-pressed=\{isSelected\}/);
  assert.match(grid,/onClick=\{\(\)=>onSelect\(coin\.symbol\)\}/);
  assert.match(grid,/coinMarketSnapshot\(coin\.symbol,markets,syncedAt\)/);
  assert.match(grid,/t\.coinPreparing/);
  assert.doesNotMatch(grid,/Chevron|chevron|ArrowDown|ArrowUp|RotateCcw/);
  assert.match(app,/marketSnapshot=\{market\?\.markets\} syncedAt=\{syncedAt\}/);
  assert.match(wallet,/marketSnapshot=\{marketSnapshot\} syncedAt=\{syncedAt\} lang=\{lang\}/);
});

test('editing or saving BTC/ETH settings cannot close selected coin panel',()=>{
  assert.match(setup,/useState\(null\)/);
  assert.match(setup,/const toggleAsset = asset => setOpened\(current=>current===asset\?null:asset\)/);
  assert.match(setup,/onSelect=\{toggleAsset\}/);
  assert.equal([...setup.matchAll(/setOpened\(/g)].length,1);
  for(const field of ['mode','stable','horizon']){
    assert.match(setup,new RegExp("onChange=\\{event=>change\\(opened,'"+field+"',event.target.value\\)\\}"));
  }
  assert.match(setup,/onClick=\{\(\)=>save\(opened\)\}/);
  assert.match(setup,/\{renderBudget\?\.\(opened\)\}/);
  assert.match(setup,/\{renderSessionControls\?\.\(opened\)\}/);
  assert.match(wallet,/onClick=\{\(\)=>saveBudget\(asset\)\}/);
  assert.match(wallet,/onChange=\{event=>setPreviewBudgets/);
  assert.match(setup,/\{selectedCoin&&<section/);
  assert.match(setup,/const selectedSettings=opened==='BTC'\|\|opened==='ETH'/);
  assert.ok(setup.indexOf('const [opened,setOpened]')<setup.indexOf('const change='));
});

test('existing privileges, independent budget persistence, wallet balances and bilingual labels are unchanged',()=>{
  assert.match(setup,/expected_revision:saved\[asset\]\.revision/);
  assert.match(wallet,/expected_revision:savedBudgets\[asset\]\.revision/);
  assert.match(setup,/data\?\.trading_enabled===false/);
  assert.match(setup,/data\?\.signing_authorized===false/);
  assert.match(wallet,/renderBudget=\{renderBudget\}/);
  assert.match(wallet,/<WalletBalances t=\{t\}/);
  assert.match(labels,/privyPriceLive: 'Live-Kurs'/);
  assert.match(labels,/privyPriceUnavailable: 'Kurs nicht verfügbar'/);
  assert.match(labels,/privyPriceLive: 'Live price'/);
  assert.doesNotMatch(setup,/signMessage|sendTransaction|sendBitcoin|delegateWallet|privateKey/);
});
