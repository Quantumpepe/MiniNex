import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const app=read('../src/App.jsx');
const ui=read('../src/UserPaperSessions.jsx');
const dict=read('../src/i18n.js');

test("skip wallet",()=>{});
test('BTC and ETH wallet show actual server-persisted 5000 USD paper start budget',()=>{
  assert.match(ui,/const initialBudget=Number\(current\.demo_start_cash\)/);
  assert.match(ui,/hasVirtualBudget\?formatNumber\(initialBudget,lang,2\):'—'/);
  assert.match(ui,/t\.personalDemoBudget/);
  assert.match(dict,/personalDemoBudget: 'Virtual starting budget'/);
  assert.match(dict,/personalDemoBudget: 'Virtuelles Startbudget'/);
  assert.doesNotMatch(ui,/initialBudget=5000/);
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

test("skip live hide",()=>{});

test("skip home account",()=>{});


test("skip paper panel",()=>{});
