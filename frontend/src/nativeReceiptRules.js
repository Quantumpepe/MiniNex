// SOL/XRPL native source-chain receipt only. This cannot certify a swap payout.
const SOL_SIG=/^[1-9A-HJ-NP-Za-km-z]{85,90}$/;
const XRP_TX=/^[A-Fa-f0-9]{64}$/;
const DIGITS=/^(?:0|[1-9][0-9]*)$/;

export function nativeReceiptRequest(asset,hash){
  if(typeof hash!=='string' ||
     (asset!=='SOL'&&asset!=='XRP') ||
     !(asset==='SOL'?SOL_SIG:XRP_TX).test(hash))return null;
  return {
    asset,hash,
    path:'/api/live/native/source-receipt/'+asset+'/'+encodeURIComponent(hash),
  };
}

export function validNativeReceiptResponse(data,request){
  if(!data||!request||typeof data!=='object'||Array.isArray(data)||
     data.mode!=='OWNER_NATIVE_SOURCE_RECEIPT_READ_ONLY'||
     data.asset!==request.asset||data.source_tx_hash!==request.hash||
     data.network!==(request.asset==='SOL'?'solana-mainnet':'xrpl-mainnet')||
     data.source_chain_finalized!==true||
     data.destination_finality_still_required!==true||
     data.cross_chain_swap_completed!==false||
     data.destination_stablecoin_received!==false||
     data.wallet_transaction_sent_by_this_check!==false||
     data.trading_profit_recorded!==false||data.fee_assessed!==false||
     data.can_submit!==false||
     ['signing_payload','inbound_address','memo','destination_tag',
       'wallet_private_key'].some(field=>field in data))return false;
  if(request.asset==='SOL'){
    return data.source_wallet_signature_verified===true &&
      Number.isSafeInteger(data.source_chain_slot)&&data.source_chain_slot>0 &&
      DIGITS.test(data.network_fee_lamports||'') &&
      DIGITS.test(data.source_wallet_balance_before_lamports||'') &&
      DIGITS.test(data.source_wallet_balance_after_lamports||'') &&
      typeof data.source_wallet_fee_payer==='boolean';
  }
  return data.source_wallet_transaction_verified===true &&
    Number.isSafeInteger(data.source_chain_ledger)&&
    data.source_chain_ledger>0 &&
    DIGITS.test(data.network_fee_drops||'');
}
