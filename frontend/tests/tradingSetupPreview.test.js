import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const setup=read('../src/TradingSetupPreview.jsx');
const panel=read('../src/PrivyWalletPanel.jsx');
const strings=read('../src/i18n.js');
const help=read('../src/HelpCenter.jsx');

test('BTC and ETH have separately persisted mode, stablecoin and horizon',()=>{
  assert.match(setup,/BTC: \{mode:'LONG', stable:'USDC', horizon:'15m'\}/);
  assert.match(setup,/ETH: \{mode:'LONG', stable:'USDC', horizon:'15m'\}/);
  assert.match(setup,/setSettings\(old=>\(\{\.\.\.old,\[asset\]:\{\.\.\.old\[asset\],\[field\]:value\}\}\)\)/);
  assert.match(setup,/\/api\/trade-preferences/);
  assert.match(setup,/expected_revision:saved\[asset\]\.revision/);
  assert.match(panel,/<TradingSetupPreview t=\{t\} getAccessToken=\{getAccessToken\}/);
});

test('spot downtrend is not leveraged shorting',()=>{
  assert.match(setup,/const MODES = \['LONG','SPOT_SHORT'\]/);
  assert.match(strings,/Spot downtrend is NOT leveraged shorting/);
  assert.match(strings,/kein gehebelter Short/);
});

test('each asset allows USDC, USDT and 5m 15m 1h 24h',()=>{
  assert.match(setup,/const STABLES = \['USDC','USDT'\]/);
  assert.match(setup,/const HORIZONS = \['5m','15m','1h','24h'\]/);
  assert.match(setup,/onChange=\{event=>change\(asset,'mode',event.target.value\)\}/);
  assert.match(setup,/onChange=\{event=>change\(asset,'stable',event.target.value\)\}/);
  assert.match(setup,/onChange=\{event=>change\(asset,'horizon',event.target.value\)\}/);
});

test('stored preferences have no signer and cannot enable real trades',()=>{
  assert.match(setup,/data\?\.trading_enabled===false/);
  assert.match(setup,/data\?\.signing_authorized===false/);
  assert.match(setup,/authorized===false/);
  assert.doesNotMatch(setup,/localStorage|sessionStorage|sendTransaction|sendBitcoin|signTransaction|delegateWallet|privateKey|approve\(/);
  assert.match(strings,/echte Handelswege und Wallet-Signaturen bleiben gesperrt/);
  assert.doesNotMatch(setup,/<InfoTip/);
  assert.match(help,/helpModesText/);
  assert.match(help,/helpHorizonText/);
});
