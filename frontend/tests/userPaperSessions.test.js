import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const app=read('../src/App.jsx');
const ui=read('../src/UserPaperSessions.jsx');
const dict=read('../src/i18n.js');

test('wallet provides identity-scoped BTC and ETH personal paper sessions',()=>{
  assert.match(app,/import UserPaperSessions from/);
  assert.match(app,/<UserPaperSessions t=\{t\}\/>/);
  assert.match(ui,/const ASSETS=\['BTC','ETH'\]/);
  assert.match(ui,/\/api\/user-paper\//);
  assert.match(ui,/Authorization:'Bearer '\+token/);
  assert.match(ui,/wallet_funds_used!==false/);
  assert.match(ui,/real_trading_enabled!==false/);
});
test('automatic paper start pause resume never calls wallet signer',()=>{
  assert.match(ui,/command\('start'\)/);
  assert.doesNotMatch(ui,/command\('evaluate'\)/);
  assert.match(ui,/setInterval\(refresh,20000\)/);
  assert.match(ui,/command\('pause'\)/);
  assert.match(ui,/command\('resume'\)/);
  assert.doesNotMatch(ui,/sendTransaction|signTransaction|requestWallet|approve\(/);
});
test('automatic server cadence and simulated funds disclosed in both languages',()=>{
  assert.match(dict,/Real wallet funds are never used/);
  assert.match(dict,/prüft der Server den Markt automatisch/);
  assert.match(dict,/1,000 virtual USD/);
  assert.match(dict,/1’000 virtuellen USD/);
});
