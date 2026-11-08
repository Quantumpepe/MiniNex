import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
const component=read('../src/BlockchainPreview.jsx');
const app=read('../src/App.jsx');
const strings=read('../src/i18n.js');
test('Blockchain preview only uses linked native BTC and embedded ETH wallet',()=>{
  assert.match(component,/chainType==='bitcoin-segwit'/);
  assert.match(component,/walletClientType==='privy'/);
  assert.match(component,/BTC_ADDRESS\.test/);
  assert.match(component,/ETH_ADDRESS\.test/);
  assert.match(component,/getAccessToken/);
  assert.match(component,/\/api\/blockchain\/history\//);
  assert.match(component,/Authorization:'Bearer '\+token/);
  assert.match(app,/<BlockchainPreview t=\{t\}\/>/);
});
test('Blockchain preview cannot write to tax ledger or authorize trading',()=>{
  assert.match(component,/wallet_ownership_verified!==false/);
  assert.match(component,/tax_complete!==false/);
  assert.doesNotMatch(component,/sendTransaction|signTransaction|\/api\/activity\/.*export/);
  assert.match(component,/\/api\/blockchain\/sync\//);
  assert.match(strings,/not a complete tax record/);
  assert.match(strings,/noch keine vollständigen Steuerbelege/);
});
