import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {validNativeDogeMainnetAddress,linkedVerifiedDogecoinAddress}
  from '../src/dogeWalletAddress.js';

const valid='DFpN6QqFfUm3gKNaxN6tNcab1FArL9cZLE';
const account=(chainType,address,client='privy')=>({
  type:'wallet',chainType,address,walletClientType:client
});
const user={id:'did:privy:owner',linkedAccounts:[
  account('ethereum','0x'+'a'.repeat(40)),
  account('bitcoin-segwit','bc1q'+'x'.repeat(38)),
  account('dogecoin',valid),
]};
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');

test('only Dogecoin native mainnet P2PKH Base58Check address is accepted',async()=>{
  assert.equal(await validNativeDogeMainnetAddress(valid),true);
  assert.equal(await validNativeDogeMainnetAddress(valid.slice(0,-1)+'A'),false);
  assert.equal(await validNativeDogeMainnetAddress('1BoatSLRHtKNngkdXEeobR76b53LETtpyT'),false);
  assert.equal(await validNativeDogeMainnetAddress('rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh'),false);
  assert.equal(await validNativeDogeMainnetAddress('0x'+'a'.repeat(40)),false);
  assert.equal(await validNativeDogeMainnetAddress('nesRpRaAbTDmZHwmzBkLd2AtF7Z9L9z5S2'),false);
  assert.equal(await validNativeDogeMainnetAddress('D'+'0'.repeat(32)),false);
  assert.equal(await validNativeDogeMainnetAddress('D'),false);
  assert.equal(await validNativeDogeMainnetAddress(null),false);
});

test('only current Privy user-owned native DOGE wallet can display',async()=>{
  assert.equal(await linkedVerifiedDogecoinAddress(user),valid);
  assert.equal(await linkedVerifiedDogecoinAddress({...user,id:null}),null);
  assert.equal(await linkedVerifiedDogecoinAddress({...user,id:'someone-else'}),null);
  assert.equal(await linkedVerifiedDogecoinAddress({id:'did:privy:other',linkedAccounts:[
    account('bitcoin-segwit',valid),account('ethereum',valid),
  ]}),null);
  assert.equal(await linkedVerifiedDogecoinAddress({id:'did:privy:other',linkedAccounts:[
    account('dogecoin',valid,'external'),
  ]}),null);
  assert.equal(await linkedVerifiedDogecoinAddress({id:'did:privy:other',linkedAccounts:[
    account('dogecoin',valid.slice(0,-1)+'A'),
  ]}),null);
});

test('DOGE UI is read-only and does not create keys or authorize transfers',()=>{
  const panel=read('../src/PrivyWalletPanel.jsx');
  const addr=read('../src/dogeWalletAddress.js');
  assert.match(panel,/if\(asset==='DOGE'\)/);
  assert.match(panel,/<ReadOnlyDogecoinWallet user=\{user\} enabled=\{verifiedAccount\} t=\{t\}\/>/);
  assert.match(panel,/linkedVerifiedDogecoinAddress\(user\)/);
  assert.match(panel,/dogeAccountsKey/);
  assert.doesNotMatch(panel,/\[enabled,user\?\.id,user\?\.linkedAccounts\]/);
  assert.match(panel,/state\.userId===user\?\.id/);
  assert.match(panel,/t\.coinAddressPending/);
  assert.match(panel,/t\.coinAddressReadOnly/);
  assert.doesNotMatch(addr,/privateKey|seedPhrase|createWallet\(|signTransaction\(|sendTransaction\(/);
});
