import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const wallet=source('../src/PrivyWalletPanel.jsx');
const setup=source('../src/TradingSetupPreview.jsx');
const balances=source('../src/WalletBalances.jsx');
const styles=source('../src/styles.css');
const labels=source('../src/i18n.js');

test('wallet starts with verified balances, then collapsible addresses',()=>{
  const balance=wallet.indexOf('<WalletBalances t={t}');
  const addresses=wallet.indexOf('<details className="privy-disclosure privy-addresses-section">');
  const trading=wallet.indexOf('<TradingSetupPreview t={t}');
  const technical=wallet.indexOf('<details className="privy-disclosure privy-technical-section">');
  const logout=wallet.indexOf('privy-wallet-logout');
  assert.ok(balance>0 && balance<addresses);
  assert.ok(addresses<trading && trading<technical && technical<logout);
  assert.match(wallet,/<summary>[\s\S]*?t\.privyAddressesTitle/);
  assert.match(wallet,/t\.privyAddressesHint/);
  assert.match(wallet,/t\.privyTechnicalTitle/);
  assert.match(wallet,/onClick=\{\(\)=>act\(logout\)\}/);
  assert.match(wallet,/navigator\?\.clipboard\?\.writeText/);
  assert.match(wallet,/<NativeBtcWallet user=\{user\} t=\{t\}\/>/);
  assert.match(wallet,/<CopyWalletAddress address=\{address\} network="ETH" t=\{t\}\/>/);
});

test('BTC and ETH settings each contain exactly their own budget slider',()=>{
  assert.match(setup,/\['BTC','ETH'\]\.map\(asset=><section key=\{asset\}/);
  assert.match(setup,/\{renderBudget\?\.\(asset\)\}/);
  assert.match(wallet,/renderBudget=\{renderBudget\}/);
  assert.match(wallet,/const renderBudget = asset =>/);
  assert.match(wallet,/value=\{previewBudgets\[asset\]\}/);
  assert.match(wallet,/onChange=\{event=>setPreviewBudgets\(old=>\(\{\.\.\.old,\[asset\]:Number\(event.target.value\)\}\)\)\}/);
  assert.match(wallet,/onClick=\{\(\)=>saveBudget\(asset\)\}/);
  assert.match(wallet,/expected_revision:savedBudgets\[asset\]\.revision/);
  assert.match(setup,/onClick=\{\(\)=>save\(asset\)\}/);
  assert.match(setup,/expected_revision:saved\[asset\]\.revision/);
  assert.match(setup,/value=\{settings\[asset\]\.mode\}/);
  assert.match(setup,/value=\{settings\[asset\]\.stable\}/);
  assert.match(setup,/value=\{settings\[asset\]\.horizon\}/);
});

test('read-only wallet data, PAPER settings and HWM stay separate',()=>{
  assert.match(wallet,/identityStatus==='verified' && databaseStatus==='stored'/);
  assert.match(wallet,/getAccessToken=\{getAccessToken\} userId=\{user\?\.id\}/);
  assert.match(wallet,/assetLedger\[asset\]\?\.high_water_mark_usd/);
  assert.match(wallet,/capabilities\.real_trading_enabled===false/);
  assert.match(wallet,/capabilities\.automated_signing_enabled===false/);
  assert.match(balances,/validateWalletBalanceResponse\(data\)/);
  assert.match(balances,/balances\[asset\]\.balance\+' '\+asset/);
  assert.doesNotMatch(wallet+setup+balances,/sendTransaction\(|sendBitcoin\(|signMessage\(|delegateWallet\(|privateKey/);
});

test('compact bilingual UI preserves mobile access and wallet network explanations',()=>{
  for(const item of ['privyAddressesTitle','privyAssetSettings','privyBudgetShortTitle',
                     'privyTechnicalTitle','privyBalancesCompactNote']){
    assert.equal((labels.match(new RegExp(item+':','g'))||[]).length,2,item);
  }
  assert.match(labels,/Wallet-Adressen/);
  assert.match(labels,/Handelseinstellungen/);
  assert.match(labels,/nicht PAPER/);
  assert.match(styles,/\.privy-balances-grid\{/);
  assert.match(styles,/grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
  assert.match(styles,/@media\(max-width:355px\)/);
  assert.match(styles,/\.privy-disclosure>summary:focus-visible/);
  assert.match(styles,/\.privy-asset-settings-fields select/);
});
