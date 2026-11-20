import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
  nativeSourcePreflightRequest,validNativeSourcePreflight,
  originalNativeBlocker,
} from '../src/nativeSourceReadinessRules.js';

const NETWORK=Object.freeze({BTC:'bitcoin-mainnet',
  SOL:'solana-mainnet',XRP:'xrpl-mainnet'});

function request(asset='BTC',side='SELL',stable='USDC',amount='0.5'){
  return {asset,side,stable,amount};
}
function response(req){
  const side=req.side;
  const basic={
    mode:'OWNER_NATIVE_SOURCE_READINESS_ONLY',
    asset:req.asset,side,stable:req.stable,input_amount:req.amount,
    source_network:side==='BUY'?'ethereum-mainnet':NETWORK[req.asset],
    destination_network:side==='BUY'?NETWORK[req.asset]:'ethereum-mainnet',
    original_native_owner_wallet_link_verified:true,
    original_native_wallet_remote_owner_verified:true,
    original_native_owner_public_key_matches_wallet:true,
    documented_privy_native_wallet_chain_type_supported:req.asset!=='XRP',
    native_privy_signer_reference_observed:true,
    original_source_preflight_verified:true,
    exact_native_privy_policy_contents_verified:false,
    user_signed_coin_budget_ledger_verified:false,
    durable_original_order_claim_verified:false,
    source_chain_broadcast_verified:false,
    crosschain_swap_filled:false,
    destination_settlement_finalized:false,
    performance_fee_assessed:false,
    wallet_signed:false,execution_enabled:false,can_submit:false,
  };
  const source={source_balance_and_network_fee_verified:true};
  const flags=side==='BUY'?{
    ethereum_chain_id_verified:true,
    source_balance_and_network_fee_verified:true,
    current_source_nonce_verified:true,
    original_router_allowance_verified:true,
    original_source_transaction_unsigned:true,
  }:req.asset==='BTC'?{
    ...source,original_funding_outpoints_verified:true,
    unsigned_bip174_psbt_compiled:true,
    bip143_message_digests_verified:true,
  }:req.asset==='SOL'?{
    ...source,solana_mainnet_genesis_verified:true,
    recent_original_blockhash_verified:true,
    original_unsigned_transfer_compiled:true,
  }:{
    ...source,xrpl_mainnet_id_verified:true,
    account_sequence_and_reserve_verified:true,
    original_unsigned_payment_compiled:true,
  };
  return {...basic,...flags};
}

for(const [asset,side,stable,amount] of [
  ['BTC','BUY','USDC','50'],['BTC','SELL','USDT','0.12345678'],
  ['SOL','BUY','USDT','12.5'],['SOL','SELL','USDC','0.123456789'],
  ['XRP','BUY','USDC','19.1'],['XRP','SELL','USDC','0.123456'],
]){
  test('validate redacted '+asset+' '+side+' '+stable+' source evidence',()=>{
    const req=request(asset,side,stable,amount);
    assert.deepEqual(nativeSourcePreflightRequest(req),{
      ...req,path:'/api/live/native/source-readiness/'+asset,
      body:{side,stable,amount},
    });
    assert.equal(validNativeSourcePreflight(response(req),req),true);
    const code=originalNativeBlocker(response(req),req);
    assert.equal(code,asset==='XRP'?'NATIVE_WALLET_UNSUPPORTED':
      'POLICY_AND_SEND_CLAIM_MISSING');
  });
}
test('never expose a readiness check for the wrong asset, stable or precision',()=>{
  const rejected=[
    request('ETH'),request('ADA'),request('XRP','SELL','USDT'),
    request('TRX','BUY','USDT'),request('BTC','HOLD'),
    request('BTC','SELL','USDC','0.123456789'),
    request('SOL','SELL','USDC','0.1234567891'),
    request('XRP','SELL','USDC','0.1234567'),
    request('SOL','BUY','USDC','0.0000001'),
    request('BTC','SELL','USDC','NaN'),
    request('BTC','SELL','USDC','1e20'),
    request('BTC','SELL','USDC','0'),
  ];
  for(const req of rejected){
    assert.equal(nativeSourcePreflightRequest(req),null,JSON.stringify(req));
  }
});
test('the owner and public key blockers are informative but never trading grants',()=>{
  const req=request('BTC');
  const base=response(req);
  for(const [field,expected] of [
    ['original_native_wallet_remote_owner_verified','REMOTE_OWNER_UNVERIFIED'],
    ['original_native_owner_public_key_matches_wallet','PUBLIC_KEY_UNVERIFIED'],
    ['native_privy_signer_reference_observed','SIGNER_NOT_ASSIGNED'],
  ]){
    const changed={...base,[field]:false};
    assert.equal(validNativeSourcePreflight(changed,req),true);
    assert.equal(originalNativeBlocker(changed,req),expected);
    assert.equal(changed.can_submit,false);
  }
});
test('a missing, true, or malformed execution field invalidates the entire payload',()=>{
  const req=request('SOL','SELL','USDC','2');
  const original=response(req);
  const forbidden=[
    'exact_native_privy_policy_contents_verified',
    'user_signed_coin_budget_ledger_verified',
    'durable_original_order_claim_verified',
    'source_chain_broadcast_verified',
    'crosschain_swap_filled','destination_settlement_finalized',
    'performance_fee_assessed','wallet_signed',
    'execution_enabled','can_submit',
  ];
  for(const field of forbidden){
    for(const value of [true,null,undefined,0]){
      assert.equal(validNativeSourcePreflight({...original,[field]:value},req),
        false,field+': '+value);
    }
  }
});
test('never trust a forged chain, amount, owner, source or unredacted payload',()=>{
  const req=request('BTC');
  const good=response(req);
  for(const [field,value] of [
    ['asset','SOL'],['side','BUY'],['stable','USDT'],
    ['input_amount','0.4'],['source_network','ethereum-mainnet'],
    ['destination_network','solana-mainnet'],
    ['original_native_owner_wallet_link_verified',false],
    ['original_source_preflight_verified',false],
    ['original_funding_outpoints_verified',false],
    ['bip143_message_digests_verified',false],
    ['original_native_wallet_remote_owner_verified','true'],
    ['documented_privy_native_wallet_chain_type_supported',null],
  ]){
    assert.equal(validNativeSourcePreflight({...good,[field]:value},req),
      false,field);
  }
  for(const field of ['memo','inbound_vault','unsigned_psbt',
    'psbt_base64','signed_wire_base64','original_compiled_message',
    'wallet_private_key','policy_id','wallet_id','source_wallet']){
    assert.equal(validNativeSourcePreflight({...good,[field]:'leak'},req),
      false,field);
  }
});
test('native check is under the quoted coin, with no additional capital controls',()=>{
  const jsx=readFileSync(new URL('../src/LiveTradePlan.jsx',import.meta.url),'utf8');
  assert.match(jsx,/\['BTC','SOL','XRP'\]\.includes\(asset\)&&<NativeSourceReadiness/);
  assert.match(jsx,/request=\{quote\.request\}/);
  const ui=readFileSync(new URL('../src/NativeSourceReadiness.jsx',import.meta.url),'utf8');
  assert.match(ui,/method:'POST'/);
  assert.match(ui,/cache:'no-store'/);
  assert.match(ui,/getAccessToken\(\)/);
  assert.doesNotMatch(ui,/sendTransaction|signAndSend|createPolicy|approve\(/);
  assert.doesNotMatch(ui,/localStorage|sessionStorage/);
  const words=readFileSync(new URL('../src/i18n.js',import.meta.url),'utf8');
  assert.equal(words.split('nativeSourceCheck:').length,3);
  assert.equal(words.split('nativeSourceNotExecutable:').length,3);
});
