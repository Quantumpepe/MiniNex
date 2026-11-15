import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {BTC_SIGNER_STATES,bitcoinSignerPreflight,bitcoinSigningIntent,
  parseBitcoinRawSignature,requestReviewedBitcoinSignature}
  from '../src/btcSignerAdapter.js';

const wallet={id:'btc-wallet',chainType:'bitcoin-segwit',
  address:'bc1q'+('q'.repeat(38))};
const pubkey='02'+'a'.repeat(64);
const hashHex='b'.repeat(64);
const signature='c'.repeat(128);
function preflight(approve=async()=>signature) {
  return bitcoinSignerPreflight({wallet,address:wallet.address,
    publicKey:pubkey,approve});
}
function intent(){
  return bitcoinSigningIntent({walletId:wallet.id,address:wallet.address,
    hashHex,reviewId:'review-1',confirmedByUser:true});
}
test('BTC signer requires a linked mainnet wallet, matching address and real callback',()=>{
  assert.equal(preflight().state,BTC_SIGNER_STATES.ready);
  assert.equal(bitcoinSignerPreflight({wallet,address:'bc1q'+('p'.repeat(38)),
    publicKey:pubkey,approve:()=>{}}).state,BTC_SIGNER_STATES.mismatch);
  assert.equal(bitcoinSignerPreflight({wallet:{...wallet,chainType:'ethereum'},
    address:wallet.address,publicKey:pubkey,approve:()=>{}}).state,
    BTC_SIGNER_STATES.mismatch);
  assert.equal(bitcoinSignerPreflight({wallet,address:wallet.address,
    publicKey:pubkey}).state,BTC_SIGNER_STATES.unsupported);
  assert.equal(bitcoinSignerPreflight({wallet,address:wallet.address,
    publicKey:'bad',approve:()=>{}}).state,BTC_SIGNER_STATES.unsupported);
  assert.equal(bitcoinSignerPreflight({wallet:{...wallet,address:'tb1qwrong'},
    address:'tb1qwrong',publicKey:pubkey,approve:()=>{}}).state,
    BTC_SIGNER_STATES.missing);
});
test('BTC signing intent cannot exist without affirmative user confirmation',()=>{
  assert.equal(bitcoinSigningIntent({walletId:wallet.id,address:wallet.address,
    hashHex,reviewId:'review-1',confirmedByUser:false}),null);
  assert.equal(bitcoinSigningIntent({walletId:wallet.id,address:wallet.address,
    hashHex:'not a hash',reviewId:'review-1',confirmedByUser:true}),null);
  assert.deepEqual(intent(),{chainType:'bitcoin-segwit',walletId:'btc-wallet',
    address:wallet.address,hashHex,reviewId:'review-1'});
});
test('BTC signature decoding rejects unknown Privy envelopes and invalid hex',()=>{
  assert.equal(parseBitcoinRawSignature('0x'+signature),signature);
  assert.equal(parseBitcoinRawSignature(signature),signature);
  assert.equal(parseBitcoinRawSignature({signature}),null);
  assert.equal(parseBitcoinRawSignature('00'),null);
  assert.equal(parseBitcoinRawSignature(signature+'ff'),null);
});
test('BTC signer calls approved adapter exactly once, verifies signature, never broadcasts',async()=>{
  let calls=0;
  const approve=async i=>{calls++;assert.deepEqual(i,intent());return signature;};
  const verified=await requestReviewedBitcoinSignature({
    preflight:preflight(approve),intent:intent(),approve,
    verifySignature:async value=>{
      assert.equal(value.hashHex,hashHex);
      assert.equal(value.publicKey,pubkey);
      assert.equal(value.signature,signature);
      return true;
    }
  });
  assert.deepEqual(verified,{ok:true,signature});
  assert.equal(calls,1);
  const forged=await requestReviewedBitcoinSignature({
    preflight:preflight(approve),intent:{...intent()},approve,
    verifySignature:async()=>true
  });
  assert.deepEqual(forged,{ok:false,reason:'signer_not_ready'});
  assert.equal(calls,1);
  const absent=await requestReviewedBitcoinSignature({
    preflight:{state:'unsupported_signer'},intent:intent(),approve,
    verifySignature:async()=>true
  });
  assert.equal(absent.ok,false);
  assert.equal(calls,1);
  assert.deepEqual(await requestReviewedBitcoinSignature({
    preflight:preflight(approve),intent:intent(),approve,
    verifySignature:async()=>false
  }),{ok:false,reason:'invalid_signature'});
});
test('current MiniNex 423 BTC wallet hook exposes address only; unsupported signer fails closed',()=>{
  // The original crashing integration incorrectly used btcWallet.wallet even
  // though useNativeBtcWallet() exposes {address,state,retry} only.
  const stable=readFileSync(new URL('../src/PrivyWalletPanel.jsx',import.meta.url),'utf8');
  assert.match(stable,/return \{address,state,retry:\(\)=>setRetry\(n=>n\+1\)\}/);
  const legacyWalletView={address:wallet.address,state:'created',retry:()=>{}};
  assert.equal(bitcoinSignerPreflight({
    wallet:legacyWalletView,address:wallet.address,
    publicKey:undefined,approve:async()=>signature
  }).state,BTC_SIGNER_STATES.mismatch);
  assert.equal(bitcoinSignerPreflight({
    wallet:{address:wallet.address,chainType:'bitcoin-segwit'},
    address:wallet.address,publicKey:undefined,
    approve:async()=>signature
  }).state,BTC_SIGNER_STATES.unsupported);
});
test('BTC signer module stays out of the stable wallet, app, and Privy hooks',()=>{
  const src=readFileSync(new URL('../src/btcSignerAdapter.js',import.meta.url),'utf8');
  for(const path of ['../src/PrivyWalletPanel.jsx','../src/main.jsx',
                     '../src/App.jsx']){
    const runtime=readFileSync(new URL(path,import.meta.url),'utf8');
    assert.doesNotMatch(runtime,/btcSignerAdapter|btcSendPreflight|BtcWalletSend/);
  }
  assert.doesNotMatch(src,/from '@privy|from "(@privy|bitcoinjs)|fetch\(|sendTransaction\(/);
});
