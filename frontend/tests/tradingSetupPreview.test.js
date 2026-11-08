import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const setup=readFileSync(new URL('../src/TradingSetupPreview.jsx',import.meta.url),'utf8');
const panel=readFileSync(new URL('../src/PrivyWalletPanel.jsx',import.meta.url),'utf8');
const strings=readFileSync(new URL('../src/i18n.js',import.meta.url),'utf8');

test('BTC and ETH each keep independent local trade setup defaults',()=>{
  assert.match(setup,/BTC: \{mode:'LONG', stable:'USDC', horizon:'15m'\}/);
  assert.match(setup,/ETH: \{mode:'LONG', stable:'USDC', horizon:'15m'\}/);
  assert.match(setup,/setSettings\(old=>\(\{\.\.\.old,\[asset\]:\{\.\.\.old\[asset\],\[field\]:value\}\}\)\)/);
  assert.match(panel,/<TradingSetupPreview t=\{t\}\/>/);
});

test('spot long and held-coin downtrend are explicitly separated from leverage',()=>{
  assert.match(setup,/const MODES = \['LONG','SPOT_SHORT'\]/);
  assert.match(strings,/Spot downtrend is NOT leveraged shorting/);
  assert.match(strings,/kein gehebelter Short/);
});

test('USDC, USDT and 15m, 1h, 24h are per-asset choices',()=>{
  assert.match(setup,/const STABLES = \['USDC','USDT'\]/);
  assert.match(setup,/const HORIZONS = \['15m','1h','24h'\]/);
  assert.match(setup,/onChange=\{event=>change\(asset,'mode',event.target.value\)\}/);
  assert.match(setup,/onChange=\{event=>change\(asset,'stable',event.target.value\)\}/);
  assert.match(setup,/onChange=\{event=>change\(asset,'horizon',event.target.value\)\}/);
});

test('trade preview never touches signed wallets, backend, persistence or live orders',()=>{
  assert.doesNotMatch(setup,/fetch\(|localStorage|sessionStorage|sendTransaction|sendBitcoin|signTransaction|delegateWallet|privateKey|approve\(/);
  assert.match(strings,/echte Handelswege und Wallet-Signaturen bleiben gesperrt/);
  assert.match(setup,/LOCAL PREVIEW|privySetupPreview/);
});
