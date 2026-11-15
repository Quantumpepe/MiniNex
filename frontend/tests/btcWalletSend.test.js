import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as bitcoin from 'bitcoinjs-lib';
import {secp256k1} from '@noble/curves/secp256k1';
import {Buffer} from 'buffer';
import {btcSatoshis,btcOwnedPublicKey,btcOwnerScript,btcRecipientScript,
  btcFeeRate,btcValidatedUtxos,btcPlan,btcDisplay,btcSignAndBroadcast,BTC_REVIEW_MAX_AGE_MS}
  from '../src/btcSendRules.js';

const source=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const priv=Buffer.alloc(32);priv[31]=1;
const publicKey=Buffer.from(secp256k1.getPublicKey(priv,true));
const address=bitcoin.payments.p2wpkh({pubkey:publicKey}).address;
const recipient=bitcoin.payments.p2wpkh({
  pubkey:Buffer.from(secp256k1.getPublicKey(Buffer.from([ ...Array(31).fill(0),2]),true))
}).address;
const script=btcOwnerScript(address,publicKey);
const utxo=[{txid:'a'.repeat(64),vout:0,value:100000n}];

test('BTC amount is exactly parsed in satoshis, without floats or arbitrary budgets',()=>{
  assert.equal(btcSatoshis('0.00000001'),1n);
  assert.equal(btcSatoshis('1.23456789'),123456789n);
  assert.equal(btcDisplay(123456789n),'1.23456789');
  for(const value of ['', '0','0.00000000','1e-8','-0.1','0.000000001','1,1','01',' 1','1.']){
    assert.equal(btcSatoshis(value),null,value);
  }
});
test('BTC user-owned public key must derive exactly the Privy mainnet P2WPKH address',()=>{
  assert.deepEqual(btcOwnedPublicKey({publicKey:publicKey.toString('hex')}),publicKey);
  assert.deepEqual(btcOwnedPublicKey({public_key:'0x'+publicKey.toString('hex')}),publicKey);
  assert.ok(btcOwnerScript(address,publicKey));
  assert.equal(btcOwnerScript(recipient,publicKey),null);
  assert.equal(btcOwnerScript('tb1qexample',publicKey),null);
  assert.equal(btcOwnedPublicKey({publicKey:'0xdeadbeef'}),null);
  assert.ok(btcRecipientScript(recipient));
  assert.equal(btcRecipientScript('tb1qexample'),null);
  assert.equal(btcRecipientScript('bc1not-a-real-address'),null);
});
test('UTXOs and provider fees must be independently verified',()=>{
  assert.deepEqual(btcValidatedUtxos([{txid:'a'.repeat(64),vout:0,value:100000,
    status:{confirmed:true}}]),utxo);
  for(const bad of [
    [{txid:'a'.repeat(64),vout:0,value:0,status:{confirmed:true}}],
    [{txid:'a'.repeat(64),vout:0,value:1000,status:{confirmed:false}}],
    [{txid:'a'.repeat(64),vout:0,value:1000,status:{confirmed:true}},
     {txid:'a'.repeat(64),vout:0,value:1000,status:{confirmed:true}}],
    [{txid:'bad',vout:0,value:1000,status:{confirmed:true}}],
  ])assert.equal(btcValidatedUtxos(bad),null);
  assert.equal(btcFeeRate({halfHourFee:8}),8);
  assert.equal(btcFeeRate({halfHourFee:0}),null);
});
test('BTC plan checks spend, fee, change and no unapproved signing',()=>{
  const p=btcPlan({destination:recipient,amount:'0.0005',utxos:utxo,
    feeRate:10,sourceScript:script});
  assert.ok(p);
  assert.equal(p.sats,50000n);
  assert.equal(p.total,100000n);
  assert.equal(p.sats+p.fee+p.change,p.total);
  assert.ok(p.fee>0n);
  assert.ok(p.createdAt<=Date.now());
  assert.equal(BTC_REVIEW_MAX_AGE_MS,90000);
  assert.equal(btcPlan({destination:recipient,amount:'1',utxos:utxo,
    feeRate:10,sourceScript:script}),null);
  assert.equal(btcPlan({destination:recipient,amount:'0.0005',utxos:utxo,
    feeRate:0,sourceScript:script}),null);
  const ui=source('../src/BtcWalletSend.jsx');
  const app=source('../src/PrivyWalletPanel.jsx');
  assert.match(ui,/onClick=\{send\}/);
  assert.match(ui,/btcSignAndBroadcast\(review/);
  assert.match(ui,/if\(!supported\|\|!review\|\|pending\.current\|\|stage!=='review'\)/);
  assert.match(ui,/Privy-Wallet/);
  assert.match(app,/<BtcWalletSend/);
  assert.match(app,/asset==='BTC'/);
  assert.doesNotMatch(ui,/autoSend|setInterval\(/);
});

test('real P2WPKH PSBT signs all inputs with Privy-style raw hashes and verifies BTC TXID',async()=>{
  const funding=new bitcoin.Transaction();
  funding.version=2;
  funding.addInput(Buffer.alloc(32),0xffffffff);
  funding.addOutput(script,100000);
  const txid=funding.getId();
  const plan=btcPlan({destination:recipient,amount:'0.0005',utxos:[
    {txid,vout:0,value:100000n}
  ],feeRate:5,sourceScript:script});
  assert.ok(plan);
  const previous=globalThis.fetch;
  let broadcast=null;
  globalThis.fetch=async(url,opts={})=>{
    if(String(url).endsWith('/tx/'+txid+'/hex')&&opts.method!=='POST')
      return new Response(funding.toHex(),{status:200});
    if(String(url).endsWith('/tx')&&opts.method==='POST'){
      broadcast=bitcoin.Transaction.fromHex(opts.body);
      return new Response(broadcast.getId(),{status:200});
    }
    throw Error('Unexpected provider route');
  };
  try{
    const result=await btcSignAndBroadcast(plan,{address,pubkey:publicKey,
      signRawHash:async({hash,chainType, address:signerAddress})=>{
        assert.equal(signerAddress,address);
        assert.equal(chainType,'bitcoin-segwit');
        return {signature:'0x'+Buffer.from(
          secp256k1.sign(Buffer.from(hash.slice(2),'hex'),priv).toCompactRawBytes()).toString('hex')};
      }});
    assert.equal(result,broadcast.getId());
    assert.equal(broadcast.ins.length,1);
    assert.equal(broadcast.outs.length,2);
    assert.deepEqual(Buffer.from(broadcast.outs[0].script),btcRecipientScript(recipient));
    assert.equal(broadcast.outs[0].value,50000);
    assert.equal(broadcast.outs[1].value,Number(plan.change));
  }finally{globalThis.fetch=previous;}
});
