import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {validXrplMainnetAddress,
  linkedVerifiedNativeAddress} from '../src/xrpAdaWalletAddresses.js';

const xrpl='rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh';

test('XRPL classic address requires Ripple alphabet, mainnet ID and double SHA checksum',async()=>{
  assert.equal(await validXrplMainnetAddress(xrpl),true);
  assert.equal(await validXrplMainnetAddress(xrpl.slice(0,-1)+'r'),false);
  assert.equal(await validXrplMainnetAddress('T'+xrpl.slice(1)),false);
  assert.equal(await validXrplMainnetAddress('X'+xrpl.slice(1)),false);
  assert.equal(await validXrplMainnetAddress('0x'+'1'.repeat(40)),false);
  assert.equal(await validXrplMainnetAddress('r'+'A'.repeat(32)),false);
});

test('only wallet accounts linked to currently authenticated Privy user can appear',async()=>{
  const user={id:'did:privy:user1',linkedAccounts:[
    {type:'wallet',chainType:'ethereum',address:'0x'+'a'.repeat(40)},
    {type:'wallet',chainType:'xrpl',address:xrpl,walletClientType:'privy'},
  ]};
  assert.equal(await linkedVerifiedNativeAddress(user,'xrpl'),xrpl);
  assert.equal(await linkedVerifiedNativeAddress({id:'user2',linkedAccounts:[]},'xrpl'),null);
  assert.equal(await linkedVerifiedNativeAddress({linkedAccounts:user.linkedAccounts},'xrpl'),null);
  assert.equal(await linkedVerifiedNativeAddress(user,'tron'),null);
  assert.equal(await linkedVerifiedNativeAddress({id:'user2',linkedAccounts:[
    {type:'wallet',chainType:'xrpl',walletClientType:'external',address:xrpl}
  ]},'xrpl'),null);
});

test('wallet previews do not invent addresses, create secrets or enable deposits',()=>{
  const source=readFileSync(new URL('../src/PrivyWalletPanel.jsx',import.meta.url),'utf8');
  const verifier=readFileSync(new URL('../src/xrpAdaWalletAddresses.js',import.meta.url),'utf8');
  assert.match(source,/linkedVerifiedNativeAddress\(user,'xrpl'\)/);
  assert.match(source,/verifiedXrp\.userId===user\?\.id/);
  assert.match(source,/asset==='XRP'/);
  assert.match(source,/useXrplWallet\(user,verifiedAccount\)/);
  assert.match(source,/t\.coinAddressPending/);
  assert.match(source,/t\.coinAddressReadOnly/);
  assert.doesNotMatch(source+verifier,/seedPhrase|privateKey|wallet_propose|createWallet\(\{chainType:'xrpl'|sendTransaction\s*\(/);
});
