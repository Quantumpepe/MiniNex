import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const wallet=readFileSync(new URL('../src/PrivyWalletPanel.jsx',import.meta.url),'utf8');
const styles=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
const langs=readFileSync(new URL('../src/i18n.js',import.meta.url),'utf8');
test('database lookup only follows valid backend Privy identity',()=>{
  assert.match(wallet,/status\?\.authenticated!==true/);
  assert.match(wallet,/status\.mode!=='IDENTITY_ONLY'/);
  assert.match(wallet,/API_BASE\+'\/api\/account'/);
  assert.match(wallet,/Authorization:'Bearer '\+token/);
});
test('read-only HWM values stay in detailed wallet page',()=>{
  assert.match(wallet,/assetLedger\[asset\]\?\.high_water_mark_usd/);
  assert.match(wallet,/\['BTC','ETH'\]\.map/);
  assert.doesNotMatch(wallet,/method:'POST'|method:'PUT'|method:'DELETE'/);
  assert.match(styles,/\.privy-hwm-box/);
});
test('5% HWM description localized and real collection disabled',()=>{
  assert.match(langs,/5% only on new realized net profit/);
  assert.match(langs,/5 % nur auf neuen realisierten Nettogewinn/);
  assert.match(langs,/No live fees collected/);
  assert.match(langs,/Keine echten Gebühren/);
});

test('failed Privy/database check is recoverable without funds or extra permissions',()=>{
  assert.match(wallet,/setRetryNonce\(n=>n\+1\)/);
  assert.match(wallet,/getAccessToken,retryNonce/);
  assert.match(wallet,/privyRetryConnection/);
  assert.doesNotMatch(wallet,/sendTransaction\(|sendBitcoin\(|delegateWallet\(/);
});

test('HWM numbers are formatted as ordinary USD amounts, not PostgreSQL exponent strings',()=>{
  assert.match(wallet,/function formatHwmUsd\(value\)/);
  assert.match(wallet,/Number\(value\)/);
  assert.match(wallet,/minimumFractionDigits: 2, maximumFractionDigits: 2/);
  assert.match(wallet,/formatHwmUsd\(assetLedger\[asset\]\?\.high_water_mark_usd\)/);
});
test('wallet introductory copy reflects integrated Privy login, not future integration',()=>{
  assert.doesNotMatch(langs,/Privy-Wallet wird erst in einer späteren Version integriert/);
  assert.doesNotMatch(langs,/Privy wallet integration will follow in a later version/);
  assert.match(langs,/Privy-Anmeldung und Ethereum-Wallet sind unten verfügbar/);
});

test('trading routes only show read-only verified disabled capability status',()=>{
  assert.match(wallet,/API_BASE\+'\/api\/trading\/capabilities'/);
  assert.match(wallet,/capabilities\.real_trading_enabled===false/);
  assert.match(wallet,/capabilities\.automated_signing_enabled===false/);
  assert.match(wallet,/capabilities\.routes\?\.BTC\?\.ready===false/);
  assert.match(wallet,/capabilities\.routes\?\.ETH\?\.ready===false/);
  assert.match(langs,/Automatische Signaturen und echte Swaps sind deaktiviert/);
});

test('BTC and ETH have independent preview budgets with no authorization or persistence',()=>{
  assert.match(wallet,/useState\(\{BTC:50,ETH:50\}\)/);
  assert.match(wallet,/\['BTC','ETH'\]\.map/);
  assert.match(wallet,/type="range" min="0" max="100" step="5"/);
  assert.match(wallet,/setPreviewBudgets\(old=>\(\{\.\.\.old,\[asset\]:Number\(event.target.value\)\}\)\)/);
  assert.match(langs,/Diese Prozente werden weder gespeichert noch freigegeben/);
  assert.doesNotMatch(wallet,/method:'POST'|method:'PUT'|method:'DELETE'/);
});
