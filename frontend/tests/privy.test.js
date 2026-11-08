import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const main=read('../src/main.jsx');
const auth=read('../src/PrivyWalletPanel.jsx');
const app=read('../src/App.jsx');
const cfg=read('../src/privyConfig.js');
const workflow=read('../../.github/workflows/frontend-check.yml');
test('official Privy provider is gated on a public app ID',()=>{
  assert.match(main,/PRIVY_APP_ID\s*\?/);
  assert.match(main,/<PrivyProvider/);
  assert.match(cfg,/VITE_PRIVY_APP_ID/);
  assert.match(cfg,/createOnLogin:\s*'all-users'/);
});
test('wallet uses embedded EVM address, not the demo wallet',()=>{
  assert.match(auth,/usePrivy/);
  assert.match(auth,/useWallets/);
  assert.match(auth,/walletClientType==='privy'/);
  assert.match(auth,/wallet\.address/);
  assert.match(app,/<PrivyWalletPanel t=\{t\}/);
  assert.match(app,/privyPaperSeparation/);
});
test('no transfers/delegations or private credentials in client wallet',()=>{
  assert.doesNotMatch(auth,/sendTransaction\s*\(|transfer\s*\(|privateKey|appSecret|delegateWallet|signMessage\s*\(/);
  assert.doesNotMatch(cfg,/VITE_PRIVY_APP_SECRET|privateKey|appSecret/);
});
test('frontend CI only validates; never publishes an APK',()=>{
  assert.match(workflow,/npm run build/);
  assert.doesNotMatch(workflow,/gh release|upload-artifact|assembleDebug/);
});
