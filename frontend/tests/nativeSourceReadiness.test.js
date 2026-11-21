import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
  nativeSourcePreflightRequest,validNativeSourcePreflight,
  originalNativeBlocker,validNativeOwnerReadback,
  nativeOwnerReadbackBlocker,validNativeBuyApprovalReport,
} from '../src/nativeSourceReadinessRules.js';

const NETWORK=Object.freeze({BTC:'bitcoin-mainnet',
  SOL:'solana-mainnet',XRP:'xrpl-mainnet',
  TRX:'tron-mainnet',DOGE:'dogecoin-mainnet'});

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
    original_native_owner_public_key_matches_wallet:side==='SELL',
    documented_privy_native_wallet_chain_type_supported:
      side==='SELL'&&req.asset!=='XRP',
    native_privy_signer_reference_observed:side==='SELL',
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
  ['TRX','BUY','USDT','19.1'],['DOGE','BUY','USDC','19.1'],
]){
  test('validate redacted '+asset+' '+side+' '+stable+' source evidence',()=>{
    const req=request(asset,side,stable,amount);
    assert.deepEqual(nativeSourcePreflightRequest(req),{
      ...req,path:'/api/live/native/source-readiness/'+asset,
      body:{side,stable,amount},
    });
    assert.equal(validNativeSourcePreflight(response(req),req),true);
    const code=originalNativeBlocker(response(req),req);
    assert.equal(code,asset==='XRP'&&side==='SELL'?
      'NATIVE_WALLET_UNSUPPORTED':'POLICY_AND_SEND_CLAIM_MISSING');
  });
}
test('never expose a readiness check for the wrong asset, stable or precision',()=>{
  const rejected=[
    request('ETH'),request('ADA'),request('XRP','SELL','USDT'),
    request('TRX','SELL','USDT'),request('DOGE','SELL','USDC'),
    request('TRX','BUY','USDC'),request('BTC','HOLD'),
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
  assert.match(jsx,/\['BTC','SOL','XRP','TRX','DOGE'\]\.includes\(asset\)&&<NativeSourceReadiness/);
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

function readback(asset='BTC'){
  return {
    mode:'PRIVY_ORIGINAL_NATIVE_OWNER_READBACK_ONLY',
    asset,network:NETWORK[asset],
    original_wallet_link_verified:true,
    original_wallet_remote_owner_verified:true,
    original_public_key_matches_wallet:true,
    privy_native_wallet_api_supported:asset!=='XRP',
    wallet_delegation_flag_reported:true,
    additional_signer_reference_present:true,
    signer_policy_reference_present:true,
    exact_remote_policy_contents_verified:false,
    per_user_owner_trade_consent_verified:false,
    original_budget_revision_verified:false,
    privy_transaction_signing_capability_verified:false,
    original_order_claim_verified:false,
    automated_signing_enabled:false,
    live_trading_enabled:false,
    execution_enabled:false,can_submit:false,
  };
}
for(const asset of ['BTC','SOL','XRP']){
  test('owner-only fallback stays separate from original chain proof for '+asset,()=>{
    const report=readback(asset);
    assert.equal(validNativeOwnerReadback(report,asset),true);
    assert.equal(nativeOwnerReadbackBlocker(report,asset),
      asset==='XRP'?'NATIVE_WALLET_UNSUPPORTED':'POLICY_AND_SEND_CLAIM_MISSING');
    assert.equal(report.can_submit,false);
    assert.equal(validNativeSourcePreflight(report,request(asset)),false);
  });
}
test('readback makes missing wallet and missing signer visible without grants',()=>{
  const btc=readback('BTC');
  const absent={...btc,original_wallet_link_verified:false,
    original_wallet_remote_owner_verified:false,
    original_public_key_matches_wallet:false,
    wallet_delegation_flag_reported:false,
    additional_signer_reference_present:false,
    signer_policy_reference_present:false};
  assert.equal(validNativeOwnerReadback(absent,'BTC'),true);
  assert.equal(nativeOwnerReadbackBlocker(absent,'BTC'),'ORIGINAL_WALLET_MISSING');
  assert.equal(nativeOwnerReadbackBlocker(
    {...btc,wallet_delegation_flag_reported:false},'BTC'),'SIGNER_NOT_ASSIGNED');
  assert.equal(nativeOwnerReadbackBlocker(
    {...btc,original_wallet_remote_owner_verified:false},'BTC'),'REMOTE_OWNER_UNVERIFIED');
  assert.equal(nativeOwnerReadbackBlocker(
    {...btc,original_public_key_matches_wallet:false},'BTC'),'PUBLIC_KEY_UNVERIFIED');
});
test('readback rejects forged privileged state or leaked signer credentials',()=>{
  const owner=readback('SOL');
  const flags=['exact_remote_policy_contents_verified',
    'per_user_owner_trade_consent_verified','original_budget_revision_verified',
    'privy_transaction_signing_capability_verified','original_order_claim_verified',
    'automated_signing_enabled','live_trading_enabled','execution_enabled','can_submit'];
  for(const k of flags){
    assert.equal(validNativeOwnerReadback({...owner,[k]:true},'SOL'),false,k);
    assert.equal(validNativeOwnerReadback({...owner,[k]:null},'SOL'),false,k);
  }
  for(const key of ['wallet_id','policy_id','authorization','source_wallet']){
    assert.equal(validNativeOwnerReadback({...owner,[key]:'secret'},'SOL'),false,key);
  }
  assert.equal(validNativeOwnerReadback({...owner,network:'ethereum-mainnet'},'SOL'),false);
  assert.equal(validNativeOwnerReadback({...owner,asset:'BTC'},'SOL'),false);
});
test('UI queries owner fallback only after source failed, never claims chain verification',()=>{
  const ui=readFileSync(new URL('../src/NativeSourceReadiness.jsx',import.meta.url),'utf8');
  assert.match(ui,/preflightCode=response\.status/);
  assert.match(ui,/if\(!response\.ok\)/);
  assert.match(ui,/\/api\/live\/native\/authorization\//);
  assert.match(ui,/validNativeOwnerReadback\(owner,nativeRequest\.asset\)/);
  assert.match(ui,/nativeOwnerReadbackBlocker\(owner,nativeRequest\.asset\)/);
  assert.match(ui,/setStatus\(\{code:ownerCode,sourceChecked:false,approvalChecked\}\)/);
  assert.match(ui,/status\.approvalChecked\?t\.nativeSourceAllowanceChecked:t\.nativeSourceNotChecked/);
  assert.match(ui,/\/api\/live\/native\/buy-approval\//);
  assert.doesNotMatch(ui,/sendTransaction|signAndSend|createPolicy|approve\(/);
  const words=readFileSync(new URL('../src/i18n.js',import.meta.url),'utf8');
  assert.equal(words.split('nativeSourceNotChecked:').length,3);
  assert.equal(words.split('nativeSourceWalletMissing:').length,3);
});

function allowanceReport(req,reset=false){
  return {
    mode:'THORCHAIN_ORIGINAL_ERC20_APPROVAL_PREVIEW_ONLY',
    asset:req.asset,side:'BUY',stable:req.stable,
    network:'ethereum-mainnet',chain_id:1,
    owner_wallet_needs_erc20_approval:true,
    existing_usdt_allowance_requires_zero_reset:reset,
    source_balance_atomic:'50000000',
    approval_amount_atomic:'50000000',
    existing_router_allowance_atomic:reset?'1000000':'0',
    source_eth_gas_balance_wei:'1000000000000000',
    required_sequential_approval_receipts:reset?2:1,
    exact_approval_gas_verified:false,
    current_remote_privy_trade_policy_verified:false,
    durable_approval_send_claimed:false,real_token_approval_sent:false,
    actual_buy_source_sent:false,source_trade_receipt_finalized:false,
    source_trade_refund_verified:false,execution_enabled:false,can_submit:false,
  };
}
test('first native BUY finite allowance is not an executable trade',()=>{
  for(const req of [
    request('BTC','BUY','USDC','50'),
    request('SOL','BUY','USDT','50'),
    request('XRP','BUY','USDC','50'),
  ]){
    const report=allowanceReport(req);
    assert.equal(validNativeBuyApprovalReport(report,req),true);
    assert.equal(validNativeBuyApprovalReport({...report,can_submit:true},req),false);
    assert.equal(validNativeBuyApprovalReport({...report,real_token_approval_sent:true},req),false);
    assert.equal(validNativeBuyApprovalReport({...report,source_balance_atomic:'49999999'},req),false);
    assert.equal(validNativeBuyApprovalReport({...report,chain_id:56},req),false);
    assert.equal(validNativeBuyApprovalReport(report,{...req,side:'SELL'}),false);
  }
});
test('partial USDT needs reset first without claiming an approval was sent',()=>{
  const req=request('SOL','BUY','USDT','50');
  const report=allowanceReport(req,true);
  assert.equal(validNativeBuyApprovalReport(report,req),true);
  assert.equal(validNativeBuyApprovalReport({...report,
    existing_usdt_allowance_requires_zero_reset:false},req),false);
  assert.equal(validNativeBuyApprovalReport({...report,
    required_sequential_approval_receipts:1},req),false);
  assert.equal(validNativeBuyApprovalReport({...report,stable:'USDC'},req),false);
});
