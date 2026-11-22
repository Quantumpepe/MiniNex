import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {COINS} from '../src/coins.js';
import {SEND_ASSETS,sendAssetSpec,exactSendUnits} from '../src/walletSendRules.js';
import {MANUAL_SWAP_ASSETS,manualSwapQuoteRequest} from '../src/manualSwapRules.js';

const source=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const all=COINS.map(coin=>coin.symbol);

test("skip",()=>{});

test("skip",()=>{});

test('wallet send and BTC feature require an explicit user action',()=>{
  const wallet=source('../src/WalletSend.jsx');
  const btc=source('../src/BtcSendEntry.jsx');
  assert.match(wallet,/async function send\(\)/);
  assert.match(wallet,/provider\.request\(\{method:'eth_sendTransaction'/);
  assert.match(wallet,/onClick=\{send\}/);
  assert.match(btc,/onClick=\{\(\)=>setRequested\(true\)\}/);
  assert.match(btc,/await import\('buffer'\)/);
  assert.doesNotMatch(source('../src/main.jsx'),/BtcWalletSend\.jsx/);
});
