// MiniNex original EVM trading scope: strict owner/wallet/chain verification.
const NETWORKS=Object.freeze({BNB:56,POL:137,AVAX:43114,LINK:1,UNI:1,ARB:42161});
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
    ['NOT_STARTED','ACTIVE','PAUSED'].includes(data.status)&&
    (data.chain_id===null||data.chain_id===NETWORKS[asset])&&
    data.new_transactions_sent===0;
}
