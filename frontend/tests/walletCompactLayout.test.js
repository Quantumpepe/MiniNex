import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const wallet=source('../src/PrivyWalletPanel.jsx');
const setup=source('../src/TradingSetupPreview.jsx');
const balances=source('../src/WalletBalances.jsx');
const styles=source('../src/styles.css');
const labels=source('../src/i18n.js');

test('wallet puts native BTC and ETH addresses in each coin instead of an extra field',()=>{
  const balance=wallet.indexOf('<WalletBalances t={t}');
  const trading=wallet.indexOf('<TradingSetupPreview t={t}');
  const technical=wallet.indexOf('<details className="privy-disclosure privy-technical-section">');
  const logout=wallet.indexOf('privy-wallet-logout');
  assert.ok(balance>0&&balance<trading);
  assert.ok(trading<technical&&technical<logout);
  assert.doesNotMatch(wallet,/privy-addresses-section/);
  assert.doesNotMatch(wallet,/t\.privyAddressesTitle|t\.privyAddressesHint/);
  assert.match(wallet,/renderCoinAddress=\{renderCoinAddress\}/);
  assert.match(setup,/\{renderCoinAddress\?\.\(opened\)\}/);
  assert.match(wallet,/const renderCoinAddress = asset =>/);
  assert.match(wallet,/onClick=\{\(\)=>act\(logout\)\}/);
  assert.match(wallet,/navigator\?\.clipboard\?\.writeText/);
  assert.match(wallet,/btcWallet\.address &&/);
  assert.match(wallet,/<CopyWalletAddress address=\{btcWallet.address\} network="BTC" t=\{t\}\/>/);
  assert.match(wallet,/<CopyWalletAddress address=\{address\} network="ETH" t=\{t\}\/>/);
  assert.match(wallet,/t\.privyTechnicalTitle/);
});


test('BTC and ETH retain separate budget sliders within the selected eight-coin panel',()=>{
  assert.match(setup,/selectedSettings=opened==='BTC'\|\|opened==='ETH'\?settings\[opened\]:null/);
  assert.match(setup,/\{selectedCoin&&<section/);
  assert.match(setup,/\{renderBudget\?\.\(opened\)\}/);
  assert.match(wallet,/renderBudget=\{renderBudget\}/);
  assert.match(wallet,/const renderBudget = asset =>/);
  assert.match(wallet,/value=\{previewBudgets\[asset\]\}/);
  assert.match(wallet,/onChange=\{event=>setPreviewBudgets\(old=>\(\{\.\.\.old,\[asset\]:Number\(event.target.value\)\}\)\)\}/);
  assert.match(wallet,/onClick=\{\(\)=>saveBudget\(asset\)\}/);
  assert.match(wallet,/expected_revision:savedBudgets\[asset\]\.revision/);
  assert.match(setup,/onClick=\{\(\)=>save\(opened\)\}/);
  assert.match(setup,/expected_revision:saved\[asset\]\.revision/);
  assert.match(setup,/value=\{selectedSettings\.mode\}/);
  assert.match(setup,/value=\{selectedSettings\.stable\}/);
  assert.match(setup,/value=\{selectedSettings\.horizon\}/);
});

test('read-only wallet data, PAPER settings and HWM stay separate',()=>{
  assert.match(wallet,/identityStatus==='verified' && databaseStatus==='stored'/);
  assert.match(wallet,/getAccessToken=\{getAccessToken\} userId=\{user\?\.id\}/);
  assert.match(wallet,/assetLedger\[asset\]\?\.high_water_mark_usd/);
  assert.match(wallet,/capabilities\.real_trading_enabled===false/);
  assert.match(wallet,/capabilities\.automated_signing_enabled===false/);
  assert.match(balances,/validateWalletBalanceResponse\(data\)/);
  assert.match(balances,/balances\[asset\]\.balance\+' '\+asset/);
  assert.match(balances,/<details className="privy-hwm-box privy-balances-card mininex-real-balances"/);
  assert.match(balances,/className="mininex-balance-summary"/);
  assert.doesNotMatch(wallet+setup+balances,/sendTransaction\(|sendBitcoin\(|signMessage\(|delegateWallet\(|privateKey/);
});

test('compact bilingual UI preserves mobile access and wallet network explanations',()=>{
  for(const item of ['coinAddressPending','privyAssetSettings','privyBudgetShortTitle',
                     'privyTechnicalTitle','privyBalancesCompactNote']){
    assert.equal((labels.match(new RegExp(item+':','g'))||[]).length,2,item);
  }
  assert.match(labels,/Wallet-Adresse noch nicht verfügbar/);
  assert.match(labels,/Handelseinstellungen/);
  assert.match(labels,/getrennt vom PAPER-Kapital/);
  assert.match(labels,/Native Bestände erscheinen nur mit verknüpfter Privy-Wallet/);
  assert.match(styles,/\.privy-balances-grid\{/);
  assert.match(styles,/grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
  assert.match(styles,/@media\(max-width:355px\)/);
  assert.match(styles,/\.privy-disclosure>summary:focus-visible/);
  assert.match(styles,/\.privy-asset-settings-fields select/);
});
