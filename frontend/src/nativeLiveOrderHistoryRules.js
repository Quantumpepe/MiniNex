// Original BTC/SOL/XRP/TRX/DOGE source and real destination journaling only.
// Never treat a SOURCE_FINALIZED cross-chain swap as an owner wallet payout.
const STATES=new Set(['PREPARED','SENDING','SUBMITTED',
  'RECONCILIATION_REQUIRED','SOURCE_FINALIZED','DESTINATION_FINALIZED',
  'REFUNDED_VERIFIED','CANCELLED']);
const SHA=/^native_[0-9a-f]{64}$/;
const DEC=/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const BTC=/^[0-9a-f]{64}$/;
const ETH=/^0x[0-9a-fA-F]{64}$/;
const SOL=/^[1-9A-HJ-NP-Za-km-z]{85,90}$/;
const XRP=/^[0-9A-Fa-f]{64}$/;
const UTXO_NATIVE=/^[0-9a-fA-F]{64}$/;
const CHAIN={BTC:'bitcoin-mainnet',SOL:'solana-mainnet',
  XRP:'xrpl-mainnet',TRX:'tron-mainnet',DOGE:'dogecoin-mainnet'};
const BUY_ONLY=new Set(['TRX','DOGE']);
const validSource=(asset,side,hash)=>side==='BUY'?ETH.test(hash):
  BUY_ONLY.has(asset)?false:
  asset==='BTC'?BTC.test(hash):asset==='SOL'?SOL.test(hash):XRP.test(hash);
const validDestination=(asset,side,state,hash)=>{
  if(side==='SELL'&&state==='DESTINATION_FINALIZED')return ETH.test(hash);
  if(side==='BUY'&&state==='REFUNDED_VERIFIED')return ETH.test(hash);
  return asset==='BTC'?BTC.test(hash):asset==='SOL'?SOL.test(hash):
    BUY_ONLY.has(asset)?UTXO_NATIVE.test(hash):XRP.test(hash);
};
export function validOriginalNativeOrderHistory(value,asset){
  if(!Object.hasOwn(CHAIN,asset)||!value||
     typeof value!=='object'||Array.isArray(value)||
     value.mode!=='OWNER_ORIGINAL_NATIVE_ORDER_HISTORY_ONLY'||
     value.asset!==asset||!Array.isArray(value.orders)||
     value.orders.length>50||typeof value.history_truncated!=='boolean'||
     value.includes_simulation!==false||
     value.wallet_signing_authorized_by_this_api!==false||
     value.automatic_execution_enabled_by_this_api!==false)return false;
  const seen=new Set();
  return value.orders.every(o=>{
    if(!o||typeof o!=='object'||Array.isArray(o)||
       o.asset!==asset||!SHA.test(o.intent_key)||seen.has(o.intent_key)||
       !['BUY','SELL'].includes(o.side)||
       (BUY_ONLY.has(asset)&&o.side!=='BUY')||!STATES.has(o.status)||
       !['USDC','USDT'].includes(o.stable)||
       (asset==='XRP'&&o.stable!=='USDC')||
       (asset==='TRX'&&o.stable!=='USDT')||
       o.source_chain!==(o.side==='BUY'?'ethereum-mainnet':CHAIN[asset])||
       !Number.isSafeInteger(o.budget_revision)||o.budget_revision<0||
       typeof o.original_input_amount!=='string'||
       !DEC.test(o.original_input_amount)||
       o.original_input_amount==='0'||
       o.original_input_asset!==(o.side==='BUY'?o.stable:asset)||
       (o.source_tx_hash!==null&&!validSource(asset,o.side,o.source_tx_hash))||
       (o.destination_tx_hash!==null&&
         !validDestination(asset,o.side,o.status,o.destination_tx_hash))||
       o.original_funds_refunded!==(o.status==='REFUNDED_VERIFIED')||
       o.destination_wallet_credited!==(o.status==='DESTINATION_FINALIZED')||
       o.source_blockchain_finalized!==[
         'SOURCE_FINALIZED','DESTINATION_FINALIZED','REFUNDED_VERIFIED'
       ].includes(o.status)||
       o.pending_or_uncertain!==[
         'SENDING','SUBMITTED','RECONCILIATION_REQUIRED','SOURCE_FINALIZED'
       ].includes(o.status)||
       o.signed_source_hash_known!==true&&o.signed_source_hash_known!==false||
       (o.expected_source_tx_hash!==null&&(
         o.side!=='SELL'||!['SENDING','RECONCILIATION_REQUIRED'].includes(o.status)||
         o.signed_source_hash_known!==true||
         !validSource(asset,'SELL',o.expected_source_tx_hash)||
         o.source_tx_hash!==null))||
       (o.expected_source_tx_hash===null&&
         o.signed_source_hash_known===true&&
         ['SENDING','RECONCILIATION_REQUIRED'].includes(o.status))||
       o.automatic_retry_allowed!==false||
       o.realized_net_pnl_usd!==null||
       o.performance_fee_assessed!==false||
       o.wallet_transaction_sent_by_this_api!==false||
       Number.isNaN(Date.parse(o.created_at))||
       Number.isNaN(Date.parse(o.updated_at)))return false;
    const settled=['DESTINATION_FINALIZED','REFUNDED_VERIFIED'].includes(o.status);
    if(settled&&(o.destination_tx_hash===null||
         !/^[1-9][0-9]*$/.test(o.destination_amount_atomic||'')||
         o.destination_asset!==(o.status==='REFUNDED_VERIFIED'?
           (o.side==='BUY'?o.stable:asset):
           (o.side==='BUY'?asset:o.stable))))return false;
    if(!settled&&(o.destination_tx_hash!==null||
         o.destination_amount_atomic!==null||
         o.destination_asset!==null))return false;
    if(['SUBMITTED','SOURCE_FINALIZED','DESTINATION_FINALIZED','REFUNDED_VERIFIED']
       .includes(o.status)&&o.source_tx_hash===null)return false;
    seen.add(o.intent_key);
    return true;
  });
}
export function nativeReceivedAmount(order){
  if(!order?.destination_amount_atomic||!order.destination_asset)return null;
  const decimals=['USDC','USDT','XRP'].includes(order.destination_asset)?6:
    ['BTC','DOGE'].includes(order.destination_asset)?8:
    order.destination_asset==='SOL'?9:order.destination_asset==='TRX'?6:null;
  if(decimals===null)return null;
  try{
    const str=BigInt(order.destination_amount_atomic).toString().padStart(decimals+1,'0');
    return str.slice(0,-decimals)+'.'+str.slice(-decimals).replace(/0+$/,'').padEnd(1,'0');
  }catch{return null;}
}
