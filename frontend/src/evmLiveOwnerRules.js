// MiniNex original EVM trading scope: strict owner/wallet/chain verification.
const NETWORKS=Object.freeze({BNB:56,POL:137,AVAX:43114,LINK:1,QNT:1,UNI:1,ARB:42161});
const ETH_ADDRESS=/^0x[0-9a-fA-F]{40}$/;
const POLICY_ID=/^[A-Za-z0-9_-]{6,160}$/;
export const LIVE_EVM_ASSETS=Object.freeze(Object.keys(NETWORKS));

export function validEvmTradeChallenge(data,{asset,address}) {
  return !!data&&NETWORKS[asset]===data.chain_id&&data.asset===asset&&
    data.mode==='EVM_OWNER_SIGNED_INTENT_ONLY'&&data.state==='PENDING'&&
    ETH_ADDRESS.test(address||'')&&
    data.wallet_address?.toLowerCase()===address.toLowerCase()&&
    typeof data.challenge==='string'&&
    data.challenge.includes('MiniNex '+asset)&&
    data.challenge.includes('Owner wallet: '+data.wallet_address)&&
    Number.isSafeInteger(data.budget_revision)&&data.budget_revision>0&&
    data.user_wallet_signature_required===true&&
    data.privy_signer_granted===false&&
    data.live_trading_enabled===false&&
    Number.isSafeInteger(data.expires_at_unix)&&
    data.expires_at_unix>Math.floor(Date.now()/1000);
}

export function validEvmTradeSignerOffer(data,{asset,address,policyId,stable}) {
  return !!data&&data.mode==='OWNER_EVM_TRADING_SIGNER_OFFER_ONLY'&&
    data.asset===asset&&data.chain_id===NETWORKS[asset]&&
    data.stable===stable&&ETH_ADDRESS.test(address||'')&&
    data.wallet_address?.toLowerCase()===address.toLowerCase()&&
    POLICY_ID.test(policyId||'')&&data.policy_id===policyId&&
    POLICY_ID.test(data.execution_signer_id||'')&&
    Number.isSafeInteger(data.budget_revision)&&data.budget_revision>0&&
    data.owner_add_signer_required===true&&
    data.combined_single_grant===true&&
    data.fee_scope?.performance_fee_percent===5&&
    data.fee_scope?.fee_basis==='REALIZED_NET_GAIN_ABOVE_HWM'&&
    data.fee_scope?.router_allowance_scoped===true&&
    ETH_ADDRESS.test(data.fee_scope?.treasury_wallet||'')&&
    /^(?:[1-9][0-9]*)$/.test(data.fee_scope?.fee_max_atomic||'')&&
    data.privy_signer_attached===false&&
    data.live_session_started===false&&data.trade_sent===false;
}

export function validEvmTradePermission(data,asset) {
  return !!data&&data.mode==='OWNER_EVM_PRIVY_PERMISSION_STATUS_ONLY'&&
    data.asset===asset&&data.chain_id===NETWORKS[asset]&&
    ['NOT_PREPARED','DRAFT','CONSENT_VERIFIED','GRANT_VERIFIED','REVOKED']
      .includes(data.status)&&
    data.can_submit===false&&
    data.auto_execution_enabled===false;
}

export function validEvmAutoSession(data,asset) {
  return !!data&&data.mode==='OWNER_EVM_LIVE_SESSION_CONTROL'&&
    data.asset===asset&&
    ['NOT_STARTED','ACTIVE','PAUSED','EXIT_PENDING','STOPPED'].includes(data.status)&&
    (data.chain_id===null||data.chain_id===NETWORKS[asset])&&
    data.new_transactions_sent===0;
}

function validTradeSourceChain(row,asset,cross){
  if(!cross)return row.chain_id===NETWORKS[asset];
  return ['BNB','POL','AVAX','ARB'].includes(asset)&&
    row.chain_id===(row.side==='BUY'?1:NETWORKS[asset]);
}

export function validEvmOrderHistory(data,asset){
  if(!data||data.mode!=='OWNER_DURABLE_EVM_ORDER_STATUS_ONLY'||
     data.asset!==asset||!LIVE_EVM_ASSETS.includes(asset)||
     data.includes_simulation!==false||
     data.wallet_signing_enabled_by_this_api!==false||
     data.automatic_trade_execution_enabled_by_this_api!==false||
     !Array.isArray(data.orders)||data.orders.length>20||
     typeof data.history_truncated!=='boolean')return false;
  const seen=new Set();
  for(const order of data.orders){
    if(!order||order.asset!==asset||!validTradeSourceChain(order,asset,order.ethereum_settlement_order===true)||
       !['BUY','SELL'].includes(order.side)||
       !['SENDING','SUBMITTED','RECONCILIATION_REQUIRED',
         'FINALIZED_SUCCESS','FINALIZED_REVERTED'].includes(order.status)||
       !/^[A-Za-z0-9_.:-]{8,128}$/.test(order.intent_key||'')||
       seen.has(order.intent_key)||
       !(order.tx_hash===null||/^0x[0-9a-f]{64}$/.test(order.tx_hash))||
       typeof order.swap_erc20_leg_finalized!=='boolean'||
       typeof order.native_trade_cycle_settled!=='boolean'||
       typeof order.fee_transferred!=='boolean'||
       typeof order.created_at!=='string'||
       !Number.isFinite(Date.parse(order.created_at))||
       !['USDC','USDT'].includes(order.stable))return false;
    if(order.swap_erc20_leg_finalized!==(order.status==='FINALIZED_SUCCESS')||
       (order.native_trade_cycle_settled&&
        (!['BNB','POL','AVAX'].includes(asset)||order.status!=='FINALIZED_SUCCESS'))||
       (['SUBMITTED','FINALIZED_SUCCESS','FINALIZED_REVERTED'].includes(order.status)
        &&order.tx_hash===null))return false;
    seen.add(order.intent_key);
  }
  return true;
}

export function validEvmFinalizedFills(data,asset){
  if(!data||data.mode!=='FINALIZED_EVM_REAL_FILLS_ONLY'||
     data.asset!==asset||!LIVE_EVM_ASSETS.includes(asset)||
     data.includes_simulation!==false||
     data.wallet_signing_authorized!==false||
     data.full_native_order_settlement_verified!==false||
     data.realized_net_pnl_calculated!==false||
     data.performance_fee_settled!==false||
     typeof data.history_truncated!=='boolean'||
     !Array.isArray(data.fills)||data.fills.length>20)return false;
  const seen=new Set();
  for(const fill of data.fills){
    if(!fill||fill.asset!==asset||!validTradeSourceChain(fill,asset,fill.ethereum_settlement_verified===true)||
       (fill.ethereum_settlement_verified===true&&
        (fill.destination_chain_id!==(fill.side==='BUY'?NETWORKS[asset]:1)||
         !/^0x[0-9a-f]{64}$/.test(fill.destination_tx_hash||'')))||
       !['BUY','SELL'].includes(fill.side)||
       !['USDC','USDT'].includes(fill.stable)||
       !/^[A-Za-z0-9_.:-]{8,128}$/.test(fill.intent_key||'')||
       seen.has(fill.intent_key)||
       !/^0x[0-9a-f]{64}$/.test(fill.tx_hash||'')||
       fill.onchain_finalized!==true||
       fill.native_coin_cycle_complete!==false||
       fill.realized_net_usd!==null||
       fill.performance_fee_paid!==false||
       fill.spent_asset!==(fill.side==='BUY'?fill.stable:asset)||
       fill.received_asset!==(fill.side==='BUY'?asset:fill.stable)||
       ![fill.spent_amount,fill.received_amount].every(v=>
          typeof v==='string'&&v.length<=96&&
          /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(v)&&
          /[1-9]/.test(v)))return false;
    seen.add(fill.intent_key);
  }
  return true;
}
