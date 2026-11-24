import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=name=>readFileSync(new URL('../src/'+name,import.meta.url),'utf8');
test('home sign-in precedes language selection in both layouts',()=>{
  assert.match(source('CompactOverview.jsx'),/<div className="compact-home-actions">\s*<HomeAccount t=\{t\}\/>\s*<div className="lang-switch"/);
  assert.match(source('App.jsx'),/<div className="header-actions">\s*\{page==='overview'&&<HomeAccount t=\{t\}\/>\}\s*<div className="lang-switch"/);
});
test('home account uses existing Privy login and directly visible logout action',()=>{
  const account=source('HomeAccount.jsx');
  assert.match(account,/\{ready,authenticated,login,logout\}=usePrivy\(\)/);
  assert.match(account,/const label=authenticated\?t\.privyLogout:t\.homeAccountLogin/);
  assert.match(account,/onClick=\{\(\)=>authenticated\?void run\(logout\):void run\(login\)\}/);
  assert.match(account,/<span>\{label\}<\/span>/);
  assert.doesNotMatch(account,/home-account-menu|setOpen/);
  assert.doesNotMatch(source('PrivyWalletPanel.jsx'),/onClick=.*(?:login|logout)\(/);
  assert.match(source('PrivyWalletPanel.jsx'),/onClick=\{\(\)=>onGoHome\?\.\(\)\}/);
});
