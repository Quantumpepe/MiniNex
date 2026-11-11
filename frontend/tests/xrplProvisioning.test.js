import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {validatedCreatedXrplAddress} from '../src/xrpAdaWalletAddresses.js';

const known='rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh';
const id='did:privy:xrp-test';
const response=(account,chain='xrpl',address=known)=>({
  user:{id:account},wallet:{chainType:chain,address}
});
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');

test('XRPL wallet creation only displays a checksum-valid Privy account-owned native address',async()=>{
  assert.equal(await validatedCreatedXrplAddress(response(id),id),known);
  assert.equal(await validatedCreatedXrplAddress(response('did:privy:other'),id),null);
  assert.equal(await validatedCreatedXrplAddress(response(id,'ethereum'),id),null);
  assert.equal(await validatedCreatedXrplAddress(response(id,'solana'),id),null);
  assert.equal(await validatedCreatedXrplAddress(response(id,'xrpl','0x'+'a'.repeat(40)),id),null);
  assert.equal(await validatedCreatedXrplAddress(response(id,'xrpl',known.slice(0,-1)+'r'),id),null);
  assert.equal(await validatedCreatedXrplAddress({wallet:{chainType:'xrpl',address:known}},id),null);
  assert.equal(await validatedCreatedXrplAddress(response(id),''),null);
  assert.equal(await validatedCreatedXrplAddress(null,id),null);
});

test('XRPL account provisioning uses authenticated Privy extended chain, no other chain reuse',()=>{
  const hook=read('../src/useXrplWallet.js');
  const panel=read('../src/PrivyWalletPanel.jsx');
  assert.match(hook,/@privy-io\/react-auth\/extended-chains/);
  assert.match(hook,/createWallet\(\{chainType:'xrpl'\}\)/);
  assert.match(hook,/validatedCreatedXrplAddress\(response,userId\)/);
  assert.match(hook,/linkedVerifiedNativeAddress\(user,'xrpl'\)/);
  assert.match(hook,/account\.current\.userId===userId&&account\.current\.enabled/);
  assert.match(hook,/if\(hasLinkedXrpl\)/);
  assert.match(panel,/useXrplWallet\(user,verifiedAccount\)/);
  assert.match(panel,/xrpWallet\.address/);
  assert.match(panel,/onClick=\{xrpWallet\.retry\}/);
  assert.match(panel,/asset==='ADA'/);
  assert.match(panel,/t\.coinAddressReadOnly/);
  assert.doesNotMatch(hook+panel,/seedPhrase|privateKey|wallet_propose|sendTransaction\s*\(|approve\s*\(/);
});
