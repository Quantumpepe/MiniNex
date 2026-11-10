import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const wallet=read('../src/PrivyWalletPanel.jsx');
const setup=read('../src/TradingSetupPreview.jsx');
const strings=read('../src/i18n.js');
const css=read('../src/styles.css');

test('no separate wallet addresses accordion remains',()=>{
  assert.doesNotMatch(wallet,/className="privy-disclosure privy-addresses-section"/);
  assert.doesNotMatch(wallet,/t\.privyAddressesTitle/);
  assert.match(wallet,/renderCoinAddress=\{renderCoinAddress\}/);
  assert.match(setup,/\{renderCoinAddress\?\.\(opened\)\}/);
});

test('BTC address is automatically prepared with account scoped Privy identity',()=>{
  assert.match(wallet,/const btcWallet = useNativeBtcWallet/);
  assert.match(wallet,/user,ready&&authenticated&&identityStatus==='verified'&&databaseStatus==='stored'/);
  assert.match(wallet,/createdWallet\?\.userId===user\?\.id/);
  assert.match(wallet,/createWallet\(\{chainType: 'bitcoin-segwit'\}\)/);
  assert.match(wallet,/isNativeBtcAddress\(wallet.address\)/);
  assert.match(wallet,/btcWallet\.state==='failed'/);
  assert.match(wallet,/onClick=\{btcWallet.retry\}/);
});

test('each opened coin shows only its own public address or a no-deposit warning',()=>{
  assert.match(wallet,/if\(asset==='BTC'\)/);
  assert.match(wallet,/if\(asset==='ETH'\)/);
  assert.match(wallet,/network="BTC"/);
  assert.match(wallet,/network="ETH"/);
  assert.match(wallet,/t\.coinAddressPending/);
  assert.match(strings,/coinAddressPending: 'Wallet-Adresse noch nicht verfügbar/);
  assert.match(strings,/coinAddressPending: 'Wallet address not available yet/);
  assert.match(wallet,/navigator\.clipboard\.writeText\(address\)/);
  assert.match(css,/\.mininex-coin-wallet-address\{/);
  assert.doesNotMatch(wallet+setup,/sendTransaction\s*\(|sendBitcoin\s*\(|delegateWallet\s*\(/);
});

test('changing coin is the only operation that changes the opened panel',()=>{
  assert.match(setup,/const toggleAsset = asset => setOpened\(current=>current===asset\?null:asset\)/);
  assert.equal([...setup.matchAll(/setOpened\(/g)].length,1);
  assert.match(setup,/\{renderCoinAddress\?\.\(opened\)\}/);
  assert.match(setup,/onClick=\{\(\)=>save\(opened\)\}/);
});
