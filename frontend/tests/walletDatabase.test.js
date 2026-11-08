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
