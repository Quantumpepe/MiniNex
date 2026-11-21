import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {nativeReceiptRequest,validNativeReceiptResponse}
  from '../src/nativeReceiptRules.js';

const read=name=>readFileSync(new URL(name,import.meta.url),'utf8');
const panel=read('../src/NativeReceiptCheck.jsx');
const trader=read('../src/LiveTradePlan.jsx');
const solHash='2'.repeat(88);
const xrpHash='F'.repeat(64);

test('native proof routes belong to selected original coin only',()=>{
  const sol=nativeReceiptRequest('SOL',solHash);
  const xrp=nativeReceiptRequest('XRP',xrpHash);
  assert.equal(sol.path,'/api/live/native/source-receipt/SOL/'+solHash);
  assert.equal(xrp.path,'/api/live/native/source-receipt/XRP/'+xrpHash);
  assert.equal(nativeReceiptRequest('SOL','0x'+'0'.repeat(64)),null);
  assert.equal(nativeReceiptRequest('XRP',solHash),null);
  assert.equal(nativeReceiptRequest('BTC',xrpHash),null);
});

function proof(request){
  const sol=request.asset==='SOL';
  return {
    mode:'OWNER_NATIVE_SOURCE_RECEIPT_READ_ONLY',
    asset:request.asset,
    network:sol?'solana-mainnet':'xrpl-mainnet',
    source_tx_hash:request.hash,
    source_chain_finalized:true,
    destination_finality_still_required:true,
    cross_chain_swap_completed:false,
    destination_stablecoin_received:false,
    wallet_transaction_sent_by_this_check:false,
    trading_profit_recorded:false,fee_assessed:false,can_submit:false,
    ...(sol
       ? {source_wallet_signature_verified:true,source_chain_slot:123,
          network_fee_lamports:'5000',source_wallet_balance_before_lamports:'12000',
          source_wallet_balance_after_lamports:'7000',
          source_wallet_fee_payer:true}
       : {source_wallet_transaction_verified:true,source_chain_ledger:123,
          network_fee_drops:'12'}),
  };
}

test('one verified SOL/XRP source is not a cross-chain payout',()=>{
  for(const [asset,hash] of [['SOL',solHash],['XRP',xrpHash]]){
    const request=nativeReceiptRequest(asset,hash);
    assert.equal(validNativeReceiptResponse(proof(request),request),true);
    for(const [field,bad] of [
      ['source_chain_finalized',false],
      ['destination_finality_still_required',false],
      ['cross_chain_swap_completed',true],
      ['destination_stablecoin_received',true],
      ['trading_profit_recorded',true],
      ['fee_assessed',true],
      ['wallet_transaction_sent_by_this_check',true],
      ['can_submit',true],
      ['source_tx_hash','another'],
      ['network','ethereum-mainnet'],
    ]){
      assert.equal(validNativeReceiptResponse({...proof(request),[field]:bad},request),
                   false,asset+' '+field);
    }
    assert.equal(validNativeReceiptResponse({...proof(request),memo:'untrusted'},request),false);
  }
});

test('receipt check never signs or transfers; both coins keep existing trade UI',()=>{
  assert.match(panel,/getAccessToken\(\)/);
  assert.match(panel,/new AbortController\(\)/);
  assert.match(panel,/validNativeReceiptResponse\(result,request\)/);
  assert.doesNotMatch(panel,/signTransaction\(|signRawHash\(|sendTransaction\(|POST/);
  assert.match(trader,/NativeReceiptCheck/);
  assert.match(trader,/asset==='ETH'&&request/);
});
