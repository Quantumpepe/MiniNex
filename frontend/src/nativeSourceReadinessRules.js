// Exact owner-only BTC/SOL/XRP on-chain preflight contract.
// This is a source-readiness observation, NEVER a trade or Privy grant.
import {LIVE_NETWORKS,LIVE_SETTLEMENTS} from './liveTradePlanRules.js';

const NATIVE=Object.freeze(['BTC','SOL','XRP']);
const DECIMAL=/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const UNSAFE=Object.freeze([
  'memo','inbound_vault','unsigned_psbt','signed_wire_base64',
  'psbt_base64','original_compiled_message','signature_digests',
  'wallet_private_key','signing_blob_hex','router','source_wallet',
  'original_intent_key','wallet_id','policy_id','authorization',
]);

const isFalse=value=>value===false;
const isBoolean=value=>typeof value==='boolean';

export function nativeSourcePreflightRequest(request){
  if(!request||!NATIVE.includes(request.asset)||
    !['BUY','SELL'].includes(request.side)||
    !LIVE_SETTLEMENTS[request.asset]?.includes(request.stable)||
    typeof request.amount!=='string'||request.amount.length>45||
    !DECIMAL.test(request.amount)||!/[1-9]/.test(request.amount))return null;
  const precision=request.side==='BUY'?6:
    request.asset==='BTC'?8:request.asset==='SOL'?9:6;
  if((request.amount.split('.')[1]||'').length>precision)return null;
  return {
    asset:request.asset,side:request.side,stable:request.stable,
    amount:request.amount,
    path:'/api/live/native/source-readiness/'+request.asset,
    body:{side:request.side,stable:request.stable,amount:request.amount},
  };
}

export function validNativeSourcePreflight(data,request){
  const checked=nativeSourcePreflightRequest(request);
  if(!checked||!data||typeof data!=='object'||Array.isArray(data)||
    data.mode!=='OWNER_NATIVE_SOURCE_READINESS_ONLY'||
    data.asset!==checked.asset||data.side!==checked.side||
    data.stable!==checked.stable||data.input_amount!==checked.amount||
    data.source_network!==(checked.side==='BUY'?'ethereum-mainnet':LIVE_NETWORKS[checked.asset])||
    data.destination_network!==(checked.side==='BUY'?LIVE_NETWORKS[checked.asset]:'ethereum-mainnet')||
    data.original_native_owner_wallet_link_verified!==true||
    data.original_source_preflight_verified!==true||
    !['original_native_wallet_remote_owner_verified',
      'original_native_owner_public_key_matches_wallet',
      'documented_privy_native_wallet_chain_type_supported',
      'native_privy_signer_reference_observed'].every(k=>isBoolean(data[k]))||
    !['exact_native_privy_policy_contents_verified',
      'user_signed_coin_budget_ledger_verified',
      'durable_original_order_claim_verified',
      'source_chain_broadcast_verified','crosschain_swap_filled',
      'destination_settlement_finalized','performance_fee_assessed',
      'wallet_signed','execution_enabled','can_submit'].every(k=>isFalse(data[k]))||
    UNSAFE.some(k=>k in data))return false;
  if(checked.side==='BUY')
    return ['ethereum_chain_id_verified',
      'source_balance_and_network_fee_verified','current_source_nonce_verified',
      'original_router_allowance_verified',
      'original_source_transaction_unsigned'].every(k=>data[k]===true);
  if(data.source_balance_and_network_fee_verified!==true)return false;
  return checked.asset==='BTC'
    ? ['original_funding_outpoints_verified','unsigned_bip174_psbt_compiled',
       'bip143_message_digests_verified'].every(k=>data[k]===true)
    : checked.asset==='SOL'
      ? ['solana_mainnet_genesis_verified','recent_original_blockhash_verified',
         'original_unsigned_transfer_compiled'].every(k=>data[k]===true)
      : ['xrpl_mainnet_id_verified','account_sequence_and_reserve_verified',
         'original_unsigned_payment_compiled'].every(k=>data[k]===true);
}

export function originalNativeBlocker(data,request){
  if(!validNativeSourcePreflight(data,request))return 'INVALID';
  if(!data.documented_privy_native_wallet_chain_type_supported)
    return 'NATIVE_WALLET_UNSUPPORTED';
  if(!data.original_native_wallet_remote_owner_verified)
    return 'REMOTE_OWNER_UNVERIFIED';
  if(!data.original_native_owner_public_key_matches_wallet)
    return 'PUBLIC_KEY_UNVERIFIED';
  if(!data.native_privy_signer_reference_observed)
    return 'SIGNER_NOT_ASSIGNED';
  return 'POLICY_AND_SEND_CLAIM_MISSING';
}


// A source-chain preflight can fail before any quoted transaction exists.
// In that case, read the original Privy wallet status separately. Do NOT
// present this fallback as a verified source-chain fee, balance or trade.
export function validNativeOwnerReadback(data,asset){
  if(!NATIVE.includes(asset)||!data||typeof data!=='object'||
    Array.isArray(data)||
    data.mode!=='PRIVY_ORIGINAL_NATIVE_OWNER_READBACK_ONLY'||
    data.asset!==asset||data.network!==LIVE_NETWORKS[asset]||
    !['original_wallet_link_verified',
      'original_wallet_remote_owner_verified',
      'original_public_key_matches_wallet',
      'privy_native_wallet_api_supported',
      'wallet_delegation_flag_reported',
      'additional_signer_reference_present',
      'signer_policy_reference_present'].every(k=>isBoolean(data[k]))||
    !['exact_remote_policy_contents_verified',
      'per_user_owner_trade_consent_verified',
      'original_budget_revision_verified',
      'privy_transaction_signing_capability_verified',
      'original_order_claim_verified','automated_signing_enabled',
      'live_trading_enabled','execution_enabled','can_submit'].every(
        k=>isFalse(data[k])
      )||
    UNSAFE.some(k=>k in data))return false;
  return true;
}

export function nativeOwnerReadbackBlocker(data,asset){
  if(!validNativeOwnerReadback(data,asset))return 'INVALID';
  if(!data.privy_native_wallet_api_supported)
    return 'NATIVE_WALLET_UNSUPPORTED';
  if(!data.original_wallet_link_verified)
    return 'ORIGINAL_WALLET_MISSING';
  if(!data.wallet_delegation_flag_reported)
    return 'SIGNER_NOT_ASSIGNED';
  if(!data.original_wallet_remote_owner_verified)
    return 'REMOTE_OWNER_UNVERIFIED';
  if(!data.original_public_key_matches_wallet)
    return 'PUBLIC_KEY_UNVERIFIED';
  if(!data.additional_signer_reference_present||
     !data.signer_policy_reference_present)
    return 'SIGNER_NOT_ASSIGNED';
  return 'POLICY_AND_SEND_CLAIM_MISSING';
}
