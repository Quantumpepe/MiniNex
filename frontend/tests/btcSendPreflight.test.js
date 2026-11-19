import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
  btcToSats,btcFromSats,btcConfirmedUtxos,btcFeeRate,btcReviewDraft
} from '../src/btcSendPreflight.js';

const mk=(v,txid='a'.repeat(64),vout=0)=>({txid,vout,value:v,status:{confirmed:true}});
test('BTC preflight exact 8-decimal satoshi amounts, no arbitrary user budget cap',()=>{
  assert.equal(btcToSats('0.00000001'),1n);
  assert.equal(btcToSats('0.12345678'),12345678n);
  assert.equal(btcToSats('250.12345678'),25012345678n);
  assert.equal(btcFromSats(25012345678n),'250.12345678');
  assert.equal(btcFromSats(0n),'0');
  for(const invalid of ['','0','-1','1e-8','01',' 2','2 ','1,2',
                        '0.000000001','1.','abc'])assert.equal(btcToSats(invalid),null);
});
test('BTC preflight only accepts confirmed unique and correctly typed UTXOs',()=>{
  assert.deepEqual(btcConfirmedUtxos([mk(1000)]),[
    {txid:'a'.repeat(64),vout:0,value:1000n}
  ]);
  assert.deepEqual(btcConfirmedUtxos([mk(5000),{...mk(1000,'b'.repeat(64)),status:{confirmed:false}}]),[
    {txid:'a'.repeat(64),vout:0,value:5000n}
  ]);
  for(const input of [[mk(0)],[mk(-1)],[mk(1,'bad')],[mk(10),mk(10)],
    [mk(1,'a'.repeat(64),-1)]])
    assert.equal(btcConfirmedUtxos(input),null);
});
test('BTC fee review conserves input sats and checks availability',()=>{
  assert.equal(btcFeeRate({halfHourFee:8}),8);
  assert.equal(btcFeeRate({halfHourFee:0}),null);
  const coins=btcConfirmedUtxos([mk(100000),mk(200000,'b'.repeat(64))]);
  const plan=btcReviewDraft({amount:'0.001',utxos:coins,feeRate:5});
  assert.ok(plan);
  assert.equal(plan.sats,100000n);
  assert.equal(plan.sats+plan.estimatedFee+plan.change,plan.total);
  assert.equal(plan.requiresFinalFeeVerification,true);
  assert.equal(btcReviewDraft({amount:'5',utxos:coins,feeRate:5}),null);
  assert.equal(btcReviewDraft({amount:'0.001',utxos:coins,feeRate:0}),null);
});
test('the stable BTC/ETH/USDC/USDT wallet never imports prototype transfer logic',()=>{
  const app=readFileSync(new URL('../src/PrivyWalletPanel.jsx',import.meta.url),'utf8');
  const entry=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');
  assert.doesNotMatch(app+entry,/btcSendPreflight|BtcWalletSend|btcSendRules/);
});
