import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const wallet=source('../src/PrivyWalletPanel.jsx');
const setup=source('../src/TradingSetupPreview.jsx');
const balances=source('../src/WalletBalances.jsx');
const styles=source('../src/styles.css');
const labels=source('../src/i18n.js');
const app=source('../src/App.jsx');

test('settings hides technical info without changing fee or ledger implementation',()=>{
  assert.doesNotMatch(app,/import WalletTechnicalInfo|<WalletTechnicalInfo/);
  assert.match(app,/<UpdateStatus lang=\{language\}\/>/);
  assert.match(source('../src/WalletTechnicalInfo.jsx'),/ledger\[asset\]\?\.high_water_mark_usd/);
});

test('wallet puts native BTC and ETH addresses in each coin instead of an extra field',()=>{
  const balance=wallet.indexOf('<WalletBalances t={t}');
  const trading=wallet.indexOf('<TradingSetupPreview t={t}');
  const technical=source('../src/WalletTechnicalInfo.jsx');
  assert.ok(balance>0&&balance<trading);
  assert.doesNotMatch(wallet,/privy-wallet-logout/);
  assert.match(source('../src/HomeAccount.jsx'),/run\(logout\)/);
  assert.match(technical,/privy-technical-section/);
  assert.doesNotMatch(wallet,/privy-addresses-section/);
  assert.doesNotMatch(wallet,/t\.privyAddressesTitle|t\.privyAddressesHint/);
  assert.match(wallet,/renderCoinAddress=\{renderCoinAddress\}/);
  assert.match(setup,/\{renderCoinAddress\?\.\(opened\)\}/);
  assert.match(wallet,/const renderCoinAddress = asset =>/);
  assert.match(source('../src/CompactOverview.jsx'),/<HomeAccount t=\{t\}\/>/);
  assert.match(wallet,/navigator\?\.clipboard\?\.writeText/);
  assert.match(wallet,/btcWallet\.address &&/);
  assert.match(wallet,/<CopyWalletAddress address=\{btcWallet.address\} network="BTC" t=\{t\}\/>/);
  assert.match(wallet,/<CopyWalletAddress address=\{address\} network="ETH" t=\{t\}\/>/);
  assert.match(technical,/t\.privyTechnicalTitle/);
});


test("skip",()=>{});

test('read-only wallet data, PAPER settings and HWM stay separate',()=>{
  assert.match(wallet,/identityStatus==='verified' && databaseStatus==='stored'/);
  assert.match(wallet,/getAccessToken=\{getAccessToken\} userId=\{user\?\.id\}/);
  assert.match(source('../src/WalletTechnicalInfo.jsx'),/ledger\[asset\]\?\.high_water_mark_usd/);
  assert.match(wallet,/capabilities\.real_trading_enabled===false/);
  assert.match(wallet,/capabilities\.automated_signing_enabled===false/);
  assert.match(balances,/validateWalletBalanceResponse\(data\)/);
  assert.match(balances,/formatAssetQuantity\(record\.balance,asset\)/);
  assert.match(balances,/<section className="mininex-stablecoin-holdings"/);
  assert.match(balances,/className="mininex-stablecoin-tiles"/);
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
