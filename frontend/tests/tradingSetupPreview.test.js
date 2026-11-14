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
  assert.match(setup,/const MODES = \['LONG','SPOT_SHORT','AUTO'\]/);
  assert.match(strings,/Spot downtrend is NOT leveraged shorting/);
  assert.match(strings,/kein gehebelter Short/);
});

test('existing BTC and ETH settings still allow stablecoins and all four horizons',()=>{
  assert.match(setup,/const STABLES = \['USDC','USDT'\]/);
  assert.match(setup,/const HORIZONS = \['5m','15m','1h','24h'\]/);
  assert.match(setup,/selectedSettings=opened==='BTC'\|\|opened==='ETH'/);
  assert.match(setup,/onChange=\{event=>change\(opened,'mode',event.target.value\)\}/);
  assert.match(setup,/onChange=\{event=>change\(opened,'stable',event.target.value\)\}/);
  assert.match(setup,/onChange=\{event=>change\(opened,'horizon',event.target.value\)\}/);
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
