import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createPrivyBtcHashSigner} from '../src/btcPrivyRawHash.js';
import {bitcoinSigningIntent,bitcoinSignerPreflight,
  requestReviewedBitcoinSignature} from '../src/btcSignerAdapter.js';

const address='bc1q'+'q'.repeat(38);
const hash='ab'.repeat(32);
const signature='cd'.repeat(64);
const wallet={address,chainType:'bitcoin-segwit'};
const pubkey='02'+'c'.repeat(64);
const review=()=>bitcoinSigningIntent({
  walletId:'wallet',address,hashHex:hash,reviewId:'approved',confirmedByUser:true
});

test('Privy adapter matches extended-chain SDK shape exactly',async()=>{
  const requests=[];
  const signer=createPrivyBtcHashSigner(async payload=>{
    requests.push(payload);
    return {signature:'0x'+signature};
  });
  assert.equal(await signer(review()),signature);
  assert.deepEqual(requests,[{address,chainType:'bitcoin-segwit',hash:'0x'+hash}]);
  assert.equal(createPrivyBtcHashSigner(null),null);
});
test('Privy adapter refuses malformed hash/chain before requesting a signature',async()=>{
  let calls=0;
  const signer=createPrivyBtcHashSigner(async()=>{calls++;return {signature};});
  for(const intent of [
    null,{}, {...review(),chainType:'ethereum'}, {...review(),hashHex:'not hex'},
    {...review(),address:'tb1q'+'q'.repeat(38)}
  ])await assert.rejects(signer(intent));
  assert.equal(calls,0);
});
test('Privy adapter refuses unsupported response envelopes',async()=>{
  const signer=createPrivyBtcHashSigner(async()=>({foo:'wrong'}));
  await assert.rejects(signer(review()),/unsupported BTC signature/);
});
test('explicit confirmation + signer preflight gate prevents unsolicited Privy calls',async()=>{
  let calls=0;
  const signer=createPrivyBtcHashSigner(async()=>{
    calls++;return {signature:'0x'+signature};
  });
  const preflight=bitcoinSignerPreflight({
    wallet,address,publicKey:pubkey,approve:signer
  });
  const blocked=await requestReviewedBitcoinSignature({
    preflight,intent:{...review()},approve:signer,verifySignature:async()=>true
  });
  assert.equal(blocked.ok,false);
  assert.equal(calls,0);
  const signed=await requestReviewedBitcoinSignature({
    preflight,intent:review(),approve:signer,verifySignature:async()=>true
  });
  assert.deepEqual(signed,{ok:true,signature});
  assert.equal(calls,1);
  const oneIntent=review();
  const first=await requestReviewedBitcoinSignature({
    preflight,intent:oneIntent,approve:signer,verifySignature:async()=>true
  });
  const second=await requestReviewedBitcoinSignature({
    preflight,intent:oneIntent,approve:signer,verifySignature:async()=>true
  });
  assert.equal(first.ok,true);
  assert.deepEqual(second,{ok:false,reason:'signer_not_ready'});
  assert.equal(calls,2);
});
test('two simultaneous confirmations for one intent never create a second signature',async()=>{
  let calls=0;
  const signer=createPrivyBtcHashSigner(async()=>{
    calls++;
    await Promise.resolve();
    return {signature:'0x'+signature};
  });
  const preflight=bitcoinSignerPreflight({wallet,address,publicKey:pubkey,approve:signer});
  const approved=review();
  const results=await Promise.all([
    requestReviewedBitcoinSignature({
      preflight,intent:approved,approve:signer,verifySignature:async()=>true
    }),
    requestReviewedBitcoinSignature({
      preflight,intent:approved,approve:signer,verifySignature:async()=>true
    })
  ]);
  assert.equal(calls,1);
  assert.equal(results.filter(result=>result.ok).length,1);
  assert.equal(results.filter(result=>!result.ok).length,1);
});
test('Privy adapter is never loaded by production App entry',()=>{
  const app=readFileSync(new URL('../src/App.jsx',import.meta.url),'utf8');
  const wallet=readFileSync(new URL('../src/PrivyWalletPanel.jsx',import.meta.url),'utf8');
  const entry=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');
  assert.doesNotMatch(app+wallet+entry,/btcPrivyRawHash|btcSignerAdapter/);
});
