import test from 'node:test';
import assert from 'node:assert/strict';
import {validOriginalNativeOrderHistory,nativeReceivedAmount}
  from '../src/nativeLiveOrderHistoryRules.js';
const key='native_'+'a'.repeat(64);
function order(asset='BTC',side='BUY',state='SOURCE_FINALIZED'){
  const dest=state==='DESTINATION_FINALIZED';
  const refunded=state==='REFUNDED_VERIFIED';
  const outAsset=dest?(side==='BUY'?asset:'USDC'):
    refunded?(side==='BUY'?'USDC':asset):null;
  return {
    asset,side,stable:'USDC',source_chain:side==='BUY'?'ethereum-mainnet':
      {BTC:'bitcoin-mainnet',SOL:'solana-mainnet',XRP:'xrpl-mainnet'}[asset],
    intent_key:key,status:state,budget_revision:1,
    original_input_amount:'100.0',
    original_input_asset:side==='BUY'?'USDC':asset,
    source_tx_hash:side==='BUY'?'0x'+'b'.repeat(64):
      asset==='SOL'?'2'.repeat(88):'c'.repeat(64),
    signed_source_hash_known:false,
    expected_source_tx_hash:null,
    destination_tx_hash:dest||refunded?
      (side==='SELL'&&dest||side==='BUY'&&refunded?
        '0x'+'e'.repeat(64):asset==='SOL'?'2'.repeat(88):
          'f'.repeat(64)):null,
    destination_amount_atomic:dest||refunded?'80000000':null,
    destination_asset:outAsset,
    source_blockchain_finalized:true,
    destination_wallet_credited:dest,original_funds_refunded:refunded,
    pending_or_uncertain:state==='SOURCE_FINALIZED',
    automatic_retry_allowed:false,realized_net_pnl_usd:null,
    performance_fee_assessed:false,
    wallet_transaction_sent_by_this_api:false,
    created_at:'2026-11-21T11:00:00+00:00',
    updated_at:'2026-11-21T11:05:00+00:00',
  };
}
function wrapper(asset,orders){
  return {mode:'OWNER_ORIGINAL_NATIVE_ORDER_HISTORY_ONLY',
    asset,orders,history_truncated:false,includes_simulation:false,
    wallet_signing_authorized_by_this_api:false,
    automatic_execution_enabled_by_this_api:false};
}
test('original confirmed source cannot appear as fully delivered trade',()=>{
  for(const asset of ['BTC','SOL','XRP']){
    const source=wrapper(asset,[order(asset)]);
    assert.equal(validOriginalNativeOrderHistory(source,asset),true);
    const fake=structuredClone(source);
    fake.orders[0].destination_wallet_credited=true;
    assert.equal(validOriginalNativeOrderHistory(fake,asset),false);
    const claimed=structuredClone(source);
    claimed.orders[0].destination_tx_hash='0x'+'e'.repeat(64);
    assert.equal(validOriginalNativeOrderHistory(claimed,asset),false);
    const credited=wrapper(asset,[order(asset,'BUY','DESTINATION_FINALIZED')]);
    assert.equal(validOriginalNativeOrderHistory(credited,asset),true);
  }
});
test('native complete SELL and source refund remain distinct',()=>{
  for(const asset of ['BTC','SOL','XRP']){
    assert.equal(validOriginalNativeOrderHistory(wrapper(asset,[
      order(asset,'SELL','DESTINATION_FINALIZED')]),asset),true);
    assert.equal(validOriginalNativeOrderHistory(wrapper(asset,[
      order(asset,'SELL','REFUNDED_VERIFIED')]),asset),true);
  }
  assert.equal(nativeReceivedAmount({destination_amount_atomic:'500000',
    destination_asset:'USDC'}),'0.5');
  assert.equal(nativeReceivedAmount({destination_amount_atomic:'123456789',
    destination_asset:'BTC'}),'1.23456789');
});
test('unrelated owner data or shadow simulation not accepted',()=>{
  let order1=order('BTC');
  const fake=wrapper('BTC',[order1,{...order1}]);
  assert.equal(validOriginalNativeOrderHistory(fake,'BTC'),false);
  for(const key of ['includes_simulation',
    'wallet_signing_authorized_by_this_api',
    'automatic_execution_enabled_by_this_api']){
    const altered=wrapper('BTC',[order1]);
    altered[key]=true;
    assert.equal(validOriginalNativeOrderHistory(altered,'BTC'),false);
  }
});


test('unknown signed source ID is visible but never shown as sent or settled',()=>{
  const forHash={BTC:'c'.repeat(64),SOL:'2'.repeat(88),XRP:'A'.repeat(64)};
  for(const asset of ['BTC','SOL','XRP']){
    const x=order(asset,'SELL','SOURCE_FINALIZED');
    x.status='RECONCILIATION_REQUIRED';
    x.source_tx_hash=null;
    x.source_blockchain_finalized=false;
    x.pending_or_uncertain=true;
    x.signed_source_hash_known=true;
    x.expected_source_tx_hash=forHash[asset];
    assert.equal(validOriginalNativeOrderHistory(wrapper(asset,[x]),asset),true);
    for(const value of [
      {...x,source_blockchain_finalized:true},
      {...x,destination_wallet_credited:true},
      {...x,automatic_retry_allowed:true},
      {...x,expected_source_tx_hash:'0x'+'e'.repeat(64)},
      {...x,side:'BUY'},
      {...x,source_tx_hash:forHash[asset]},
    ]){
      assert.equal(validOriginalNativeOrderHistory(wrapper(asset,[value]),asset),false);
    }
  }
});
