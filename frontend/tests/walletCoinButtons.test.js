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
  assert.match(grid,/t\.privyPriceUnavailable/);
  assert.match(grid,/live\?formatMoney\(q\.price_usdt,lang\)/);
  assert.doesNotMatch(grid,/Chevron|chevron|ArrowDown|ArrowUp|RotateCcw/);
  assert.match(app,/marketSnapshot=\{market\?\.markets\} syncedAt=\{syncedAt\}/);
  assert.match(wallet,/marketSnapshot=\{marketSnapshot\} syncedAt=\{syncedAt\} lang=\{lang\}/);
});

test("skip",()=>{});

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

test('wallet actions remain inside selected coin, technical info is hidden from Settings',()=>{
  const settingsPage=read('../src/App.jsx');
  const send=read('../src/WalletSend.jsx');
  const swap=read('../src/ManualSwapPreview.jsx');
  const technical=read('../src/WalletTechnicalInfo.jsx');
  assert.match(setup,/renderCoinActions\?\.\(opened\)/);
  assert.match(wallet,/renderCoinActions = asset/);
  assert.match(wallet,/<WalletSend key=\{'send-'\+asset\} initialCoin=\{asset\} lockedCoin/);
  assert.match(wallet,/<ManualSwapPreview key=\{'swap-'\+asset\} initialCoin=\{asset\} lockedCoin/);
  assert.doesNotMatch(wallet,/<details className="privy-disclosure privy-technical-section">/);
  assert.doesNotMatch(settingsPage,/<WalletTechnicalInfo t=\{t\}\/>/);
  assert.match(technical,/privyTechnicalTitle/);
  assert.match(send,/lockedCoin\?<strong>\{coin\}<\/strong>/);
  assert.match(swap,/const visibleItems=lockedCoin/);
});
