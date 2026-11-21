import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as bitcoin from 'bitcoinjs-lib';
import {secp256k1} from '@noble/curves/secp256k1';
import {Buffer} from 'buffer';
import {btcSatoshis,btcOwnedPublicKey,btcOwnerScript,btcRecipientScript,
  btcFeeRate,btcValidatedUtxos,btcPlan,btcMaxPlan,btcDisplay,btcSignAndBroadcast,btcRecoverPublicKey,BTC_REVIEW_MAX_AGE_MS}
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
test('Privy challenge signature recovers a public key only for its own BTC address',()=>{
  const digest=Buffer.from('MiniNex BTC challenge verification hash'.slice(0,32));
  const signature=secp256k1.sign(digest,priv).toCompactRawBytes();
  const hex='0x'+Buffer.from(signature).toString('hex');
  assert.deepEqual(btcRecoverPublicKey(address,digest,hex),publicKey);
  assert.deepEqual(btcRecoverPublicKey(address,digest,hex+'00'),publicKey);
  assert.equal(btcRecoverPublicKey(recipient,digest,hex),null);
  assert.equal(btcRecoverPublicKey('bc1p'+'q'.repeat(58),digest,hex),null);
  assert.equal(btcRecoverPublicKey(address,Buffer.alloc(8),hex),null);
  assert.equal(btcRecoverPublicKey(address,digest,'deadbeef'),null);
});
test('BTC crypto is lazy-loaded only from user action with a render fallback',()=>{
  const entry=source('../src/BtcSendEntry.jsx');
  const startup=source('../src/PrivyWalletPanel.jsx');
  const main=source('../src/main.jsx');
  assert.match(entry,/const loadBtcSend=async\(\)=>/);
  assert.match(entry,/await import\('buffer'\)/);
  assert.match(entry,/globalThis\.Buffer=Buffer/);
  assert.match(entry,/return import\('\.\/BtcWalletSend\.jsx'\)/);
  assert.ok(entry.indexOf("globalThis.Buffer=Buffer") < entry.indexOf("return import('./BtcWalletSend.jsx')"));
  assert.match(entry,/React.lazy\(loadBtcSend\)/);
  assert.match(entry,/getDerivedStateFromError/);
  assert.match(entry,/onClick=\{\(\)=>setRequested\(true\)\}/);
  assert.doesNotMatch(startup+main,/import .*btcSendRules|import .*BtcWalletSend\.jsx/);
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
  assert.match(app,/<BtcSendEntry/);
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


test('manual BTC MAX consumes all confirmed inputs, preserving exact sats and reviewed fee',()=>{
  const two=[
    {txid:'a'.repeat(64),vout:0,value:100000n},
    {txid:'b'.repeat(64),vout:1,value:70000n},
  ];
  const plan=btcMaxPlan({destination:recipient,utxos:two,
    feeRate:7,sourceScript:script});
  assert.ok(plan);
  assert.equal(plan.utxos.length,2);
  assert.equal(plan.total,170000n);
  assert.equal(plan.change,0n);
  assert.equal(plan.sats+plan.fee,plan.total);
  assert.equal(btcSatoshis(plan.amount),plan.sats);
  assert.equal(btcDisplay(plan.sats),plan.amount);
  assert.equal(btcDisplay(2100000000000000n),'21000000');
  assert.equal(btcMaxPlan({destination:recipient,utxos:two,
    feeRate:50000,sourceScript:script}),null);
  assert.equal(btcMaxPlan({destination:recipient,utxos:[...two,two[0]],
    feeRate:1,sourceScript:script}),null);
  const ui=source('../src/BtcWalletSend.jsx');
  assert.match(ui,/btcMaxPlan\(\{destination,utxos,feeRate:rate/);
  assert.match(ui,/onClick=\{event=>check\(event,true\)\}/);
});

test('manual MAX signs all owned Bitcoin inputs and creates a single output',async()=>{
  const funding=new bitcoin.Transaction();
  funding.version=2;
  funding.addInput(Buffer.alloc(32),0xffffffff);
  funding.addOutput(script,100000);
  funding.addOutput(script,200000);
  const txid=funding.getId();
  const plan=btcMaxPlan({destination:recipient,utxos:[
    {txid,vout:0,value:100000n},
    {txid,vout:1,value:200000n}
  ],feeRate:5,sourceScript:script});
  assert.ok(plan);
  const previous=globalThis.fetch;
  let posted=null;
  globalThis.fetch=async(url,opts={})=>{
    if(String(url).endsWith('/tx/'+txid+'/hex')&&opts.method!=='POST')
      return new Response(funding.toHex(),{status:200});
    if(String(url).endsWith('/tx')&&opts.method==='POST'){
      posted=bitcoin.Transaction.fromHex(opts.body);
      return new Response(posted.getId(),{status:200});
    }
    throw Error('Unexpected test provider endpoint');
  };
  try{
    const actual=await btcSignAndBroadcast(plan,{address,pubkey:publicKey,
      signRawHash:async({hash,chainType})=>{
        assert.equal(chainType,'bitcoin-segwit');
        return {signature:'0x'+Buffer.from(secp256k1.sign(
          Buffer.from(hash.slice(2),'hex'),priv).toCompactRawBytes()).toString('hex')};
      }});
    assert.equal(actual,posted.getId());
    assert.equal(posted.ins.length,2);
    assert.equal(posted.outs.length,1);
    assert.equal(BigInt(posted.outs[0].value),plan.sats);
    assert.deepEqual(Buffer.from(posted.outs[0].script),btcRecipientScript(recipient));
  }finally{globalThis.fetch=previous;}
});


test('signed Bitcoin TXID is exposed before the broadcast reply becomes uncertain',async()=>{
  const funding=new bitcoin.Transaction();
  funding.version=2;
  funding.addInput(Buffer.alloc(32),0xffffffff);
  funding.addOutput(script,100000);
  const txid=funding.getId();
  const plan=btcMaxPlan({destination:recipient,utxos:[
    {txid,vout:0,value:100000n}
  ],feeRate:5,sourceScript:script});
  assert.ok(plan);
  const before=globalThis.fetch;
  let sentHash=null;
  let attemptedTxid=null;
  globalThis.fetch=async(url,opts={})=>{
    if(String(url).endsWith('/tx/'+txid+'/hex')&&opts.method!=='POST')
      return new Response(funding.toHex(),{status:200});
    if(String(url).endsWith('/tx')&&opts.method==='POST'){
      attemptedTxid=bitcoin.Transaction.fromHex(opts.body).getId();
      throw Error('Network response lost after possible broadcast');
    }
    throw Error('Unexpected test provider request');
  };
  try{
    await assert.rejects(()=>btcSignAndBroadcast(plan,{address,pubkey:publicKey,
      signRawHash:async({hash})=>({signature:'0x'+Buffer.from(
        secp256k1.sign(Buffer.from(hash.slice(2),'hex'),priv).toCompactRawBytes()
      ).toString('hex')}),
      onSigned:hash=>{sentHash=hash}
    }));
    assert.equal(sentHash,attemptedTxid);
    assert.match(sentHash,/^[a-f0-9]{64}$/);
    assert.match(source('../src/BtcWalletSend.jsx'),/stage==='uncertain'&&txid/);
  }finally{globalThis.fetch=before;}
});


test('BTC Send persists only a signed public TXID for recovery after OTA reload',()=>{
  const ui=source('../src/BtcWalletSend.jsx');
  assert.match(ui,/mininex:last-signed-btc:/);
  assert.match(ui,/globalThis\.localStorage\?\.setItem\(signedKey,hash\)/);
  assert.match(ui,/globalThis\.localStorage\?\.getItem\(signedKey\)/);
  assert.match(ui,/rememberSignedTxid\(hash\)/);
  assert.match(ui,/lastSignedTxid!==txid/);
  assert.doesNotMatch(ui,/localStorage\?\.setItem\([^\n]*signedWire/);
});
