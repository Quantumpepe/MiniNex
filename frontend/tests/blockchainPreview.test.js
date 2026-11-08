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
  assert.match(component,/\/api\/blockchain\/preview\//);
  assert.match(component,/Authorization:'Bearer '\+token/);
  assert.match(app,/<BlockchainPreview t=\{t\}\/>/);
});
test('Blockchain preview cannot write to tax ledger or authorize trading',()=>{
  assert.match(component,/wallet_ownership_verified!==false/);
  assert.match(component,/tax_complete!==false/);
  assert.doesNotMatch(component,/sendTransaction|signTransaction|POST|PUT|\/api\/activity\/.*export/);
  assert.match(strings,/not yet part of the tax export/);
  assert.match(strings,/noch nicht im Steuerexport enthalten/);
});
