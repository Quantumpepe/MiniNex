import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {validXrplMainnetAddress,validCardanoMainnetPaymentAddress,
  linkedVerifiedNativeAddress} from '../src/xrpAdaWalletAddresses.js';

const BECH32='qpzry9x8gf2tvdw0s3jn54khce6mua7l';

function polymod(data) {
  let check=1;
  for(const v of data){
    const top=check>>>25;
    check=((check&0x1ffffff)<<5)^v;
    [0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3]
      .forEach((g,i)=>{if((top>>>i)&1)check^=g;});
  }
  return check>>>0;
}

function makeCardanoAddress(type=6,network=1,hrp='addr') {
  const byteLength=type===0?57:29;
  const bytes=new Uint8Array(byteLength);
  bytes[0]=(type<<4)|network;
  for(let i=1;i<bytes.length;i++) bytes[i]=(i*17)%256;
  let bits=0,acc=0;
  const values=[];
  for(const b of bytes){
    acc=(acc<<8)|b;bits+=8;
    while(bits>=5){bits-=5;values.push((acc>>>bits)&31);acc&=(1<<bits)-1;}
  }
  if(bits)values.push((acc<<(5-bits))&31);
  const expanded=[...[...hrp].map(c=>c.charCodeAt(0)>>>5),0,
    ...[...hrp].map(c=>c.charCodeAt(0)&31)];
  const pm=polymod([...expanded,...values,0,0,0,0,0,0])^1;
  const check=[25,20,15,10,5,0].map(shift=>(pm>>>shift)&31);
  return hrp+'1'+[...values,...check].map(v=>BECH32[v]).join('');
}

const xrpl='rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh';

test('XRPL classic address requires Ripple alphabet, mainnet ID and double SHA checksum',async()=>{
  assert.equal(await validXrplMainnetAddress(xrpl),true);
  assert.equal(await validXrplMainnetAddress(xrpl.slice(0,-1)+'r'),false);
  assert.equal(await validXrplMainnetAddress('T'+xrpl.slice(1)),false);
  assert.equal(await validXrplMainnetAddress('X'+xrpl.slice(1)),false);
  assert.equal(await validXrplMainnetAddress('0x'+'1'.repeat(40)),false);
  assert.equal(await validXrplMainnetAddress('r'+'A'.repeat(32)),false);
});

test('Cardano address accepts mainnet, payment-key based, checksum-valid addresses',()=>{
  const enterprise=makeCardanoAddress(6,1);
  const base=makeCardanoAddress(0,1);
  assert.equal(validCardanoMainnetPaymentAddress(enterprise),true);
  assert.equal(validCardanoMainnetPaymentAddress(base),true);
  assert.equal(validCardanoMainnetPaymentAddress(enterprise.slice(0,-1)+'q'),false);
  assert.equal(validCardanoMainnetPaymentAddress(makeCardanoAddress(6,0)),false);
  assert.equal(validCardanoMainnetPaymentAddress(makeCardanoAddress(7,1)),false);
  assert.equal(validCardanoMainnetPaymentAddress(makeCardanoAddress(6,1,'addr_test')),false);
  assert.equal(validCardanoMainnetPaymentAddress('stake1'+'q'.repeat(60)),false);
  assert.equal(validCardanoMainnetPaymentAddress(xrpl),false);
});

test('only wallet accounts linked to currently authenticated Privy user can appear',async()=>{
  const ada=makeCardanoAddress(6,1);
  const user={id:'did:privy:user1',linkedAccounts:[
    {type:'wallet',chainType:'ethereum',address:'0x'+'a'.repeat(40)},
    {type:'wallet',chainType:'xrpl',address:xrpl,walletClientType:'privy'},
    {type:'wallet',chainType:'cardano',address:ada,walletClientType:'privy'},
  ]};
  assert.equal(await linkedVerifiedNativeAddress(user,'xrpl'),xrpl);
  assert.equal(await linkedVerifiedNativeAddress(user,'cardano'),ada);
  assert.equal(await linkedVerifiedNativeAddress({id:'user2',linkedAccounts:[]},'xrpl'),null);
  assert.equal(await linkedVerifiedNativeAddress({linkedAccounts:user.linkedAccounts},'xrpl'),null);
  assert.equal(await linkedVerifiedNativeAddress(user,'tron'),null);
  assert.equal(await linkedVerifiedNativeAddress({id:'user2',linkedAccounts:[
    {type:'wallet',chainType:'xrpl',walletClientType:'external',address:xrpl}
  ]},'xrpl'),null);
  assert.equal(await linkedVerifiedNativeAddress({id:'user2',linkedAccounts:[
    {type:'wallet',chainType:'cardano',walletClientType:'privy',address:makeCardanoAddress(6,0)}
  ]},'cardano'),null);
});

test('wallet previews do not invent addresses, create secrets or enable deposits',()=>{
  const source=readFileSync(new URL('../src/PrivyWalletPanel.jsx',import.meta.url),'utf8');
  const verifier=readFileSync(new URL('../src/xrpAdaWalletAddresses.js',import.meta.url),'utf8');
  assert.match(source,/linkedVerifiedNativeAddress\(user,'xrpl'\)/);
  assert.match(source,/linkedVerifiedNativeAddress\(user,'cardano'\)/);
  assert.match(source,/verifiedXrpAda\.userId===user\?\.id/);
  assert.match(source,/asset==='XRP'\|\|asset==='ADA'/);
  assert.match(source,/t\.coinAddressPending/);
  assert.match(source,/t\.coinAddressReadOnly/);
  assert.doesNotMatch(source+verifier,/seedPhrase|privateKey|wallet_propose|createWallet\(\{chainType:'xrpl'|sendTransaction\s*\(/);
});
