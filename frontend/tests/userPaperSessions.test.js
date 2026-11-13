import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const app=read('../src/App.jsx');
const ui=read('../src/UserPaperSessions.jsx');
const dict=read('../src/i18n.js');

test('wallet provides identity-scoped BTC and ETH personal paper sessions',()=>{
  assert.match(app,/import UserPaperSessions from/);
  assert.match(app,/<UserPaperSessions t=\{t\} lang=\{language\}\/>/);
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
  assert.match(dict,/5,000 virtual USD/);
  assert.match(dict,/5’000 virtuellen USD/);
});

test('live mode hides simulated home timeline and keeps personal PAPER controls disabled',()=>{
  const compact=read('../src/CompactOverview.jsx');
  assert.match(ui,/\/api\/user-paper\/availability/);
  assert.match(ui,/if\(!paperVisible\)return null/);
  assert.match(app,/\{paperVisible&&<section className="panel recent-panel"/);
  assert.match(compact,/\{showSimulatedTrades&&<section className="compact-recent-trades"/);
  assert.match(app,/useAllPaperHomeTrades\(paperVisible,page==='overview',refreshNonce,homePaperScope\)/);
});

test('home uses only the current Privy account, never shared demo or LIVE fills',()=>{
  const hook=read('../src/useAllPaperHomeTrades.js');
  const validator=read('../src/homePaperFeed.js');
  assert.match(hook,/const did=user\?\.id\|\|null/);
  assert.match(hook,/Authorization:'Bearer '\+token/);
  assert.match(hook,/API_BASE\+'\/api\/home\/paper-trades\?asset='/);
  assert.match(hook,/validHomePaperFeed\(data,assetScope\)/);
  assert.match(hook,/controllers\.clear\(\)/);
  assert.match(validator,/data\.scope!=='PRIVATE_ACCOUNT'/);
  assert.match(validator,/data\.wallet_funds_used!==false/);
  assert.match(validator,/trade\.real_trade!==false/);
  assert.match(app,/history=\{history\} trades=\{homePaperTrades\.trades\}/);
  assert.match(app,/<ActivityList trades=\{homePaperTrades\.trades\}/);
  assert.doesNotMatch(app,/<ActivityList trades=\{trades\}/);
});
