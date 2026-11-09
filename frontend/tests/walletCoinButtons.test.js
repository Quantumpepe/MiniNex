import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const setup=read('../src/TradingSetupPreview.jsx');
const wallet=read('../src/PrivyWalletPanel.jsx');
const app=read('../src/App.jsx');
const style=read('../src/styles.css');
const labels=read('../src/i18n.js');

test('BTC and ETH render independent real buttons with icons and verified live prices but no chevrons',()=>{
  assert.match(setup,/\['BTC','ETH'\]\.map\(asset=>\{/);
  assert.match(setup,/<button type="button" className="privy-asset-toggle"/);
  assert.match(setup,/onClick=\{\(\)=>toggleAsset\(asset\)\}/);
  assert.match(setup,/aria-expanded=\{open\}/);
  assert.match(setup,/aria-controls=\{'mininex-wallet-settings-'\+asset\}/);
  assert.match(setup,/<Bitcoin size=\{21\}\/>/);
  assert.match(setup,/className="compact-eth">◆/);
  assert.match(setup,/asset==='BTC'\?'Bitcoin':'Ethereum'/);
  assert.match(setup,/formatMoney\(quote\.price_usdt,lang\)/);
  assert.match(setup,/validMarket\(quote\)/);
  assert.match(setup,/Date\.now\(\)-syncedAt<20000/);
  assert.match(setup,/fresh\?t\.privyPriceLive:t\.privyPriceUnavailable/);
  assert.doesNotMatch(setup,/Chevron|chevron|ArrowDown|ArrowUp|RotateCcw/);
  assert.match(app,/marketSnapshot=\{market\?\.markets\} syncedAt=\{syncedAt\}/);
  assert.match(wallet,/marketSnapshot=\{marketSnapshot\} syncedAt=\{syncedAt\} lang=\{lang\}/);
});

test('editing and saving settings never close an expanded asset',()=>{
  assert.match(setup,/useState\(\{BTC:false,ETH:false\}\)/);
  assert.match(setup,/const toggleAsset = asset => setOpened\(old=>\(\{\.\.\.old,\[asset\]:!old\[asset\]\}\)\)/);
  // Only the coin button calls toggleAsset. Changing select, slider or saving
  // preferences cannot affect the open/closed state.
  const toggles=[...setup.matchAll(/toggleAsset\(asset\)/g)];
  assert.equal(toggles.length,1);
  assert.equal([...setup.matchAll(/setOpened\(/g)].length,1);
  assert.match(setup,/onChange=\{event=>change\(asset,'mode',event\.target\.value\)\}/);
  assert.match(setup,/onChange=\{event=>change\(asset,'stable',event\.target\.value\)\}/);
  assert.match(setup,/onChange=\{event=>change\(asset,'horizon',event\.target\.value\)\}/);
  assert.match(setup,/onClick=\{\(\)=>save\(asset\)\}/);
  assert.match(wallet,/onClick=\{\(\)=>saveBudget\(asset\)\}/);
  assert.match(wallet,/onChange=\{event=>setPreviewBudgets/);
  // Controlled form remains mounted when hidden; no remount on price refresh.
  assert.match(setup,/hidden=\{!open\}/);
  assert.match(style,/\.privy-asset-settings-content\[hidden\]\{display:none!important\}/);
  assert.match(setup,/<div id=\{'mininex-wallet-settings-'\+asset\}/);
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
