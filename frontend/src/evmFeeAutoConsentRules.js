// Exact, user-selected fee transfer cap. Never round up a value silently.
const NETWORKS = Object.freeze({BNB:56, POL:137, AVAX:43114, LINK:1, UNI:1});
const STABLES = Object.freeze({
  BNB:['USDT'], POL:['USDC'], AVAX:['USDC'],
  LINK:['USDC','USDT'], UNI:['USDC','USDT'],
});
const ETH_ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const ID = /^[A-Za-z0-9_-]{6,160}$/;

export const EVM_FEE_COINS = Object.freeze(Object.keys(NETWORKS));

export function exactFeeCapAtomic(value, asset, stable) {
  if(typeof value!=='string'||!STABLES[asset]?.includes(stable))return null;
  const parts=/^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/.exec(value.trim());
  if(!parts)return null;
  const decimals=asset==='BNB'?18:6;
  if((parts[2]||'').length>decimals)return null;
  try{
    const base=10n**BigInt(decimals);
    const atomics=BigInt(parts[1])*base+
      BigInt((parts[2]||'').padEnd(decimals,'0')||'0');
    return atomics>0n&&atomics<10n**45n?atomics.toString():null;
  }catch{return null;}
}

export function validFeeAutoChallenge(data,{asset,stable,address,cap}) {
  return !!data&&data.mode==='EVM_OWNER_FEE_ENROLLMENT_INTENT_ONLY'&&
    data.asset===asset&&data.stable===stable&&
    data.chain_id===NETWORKS[asset]&&
    typeof address==='string'&&ETH_ADDRESS.test(address)&&
    data.wallet_address?.toLowerCase()===address.toLowerCase()&&
    ETH_ADDRESS.test(data.treasury_wallet||'')&&
    data.treasury_wallet.toLowerCase()!==address.toLowerCase()&&
    data.owner_max_fee_atomic===cap&&
    typeof data.policy_sha256==='string'&&/^[0-9a-f]{64}$/.test(data.policy_sha256)&&
    typeof data.message_to_sign==='string'&&data.message_to_sign.length>50&&
    data.owner_wallet_eip191_signature_required===true&&
    data.privy_grant_attached===false&&
    data.fee_auto_transfer_enabled===false;
}

export function validFeeAutoGrantOffer(data,{asset,stable,address,policyId,cap}) {
  return !!data&&data.mode==='OWNER_EVM_FEE_SIGNER_GRANT_OFFER_ONLY'&&
    data.asset===asset&&data.stable===stable&&
    data.chain_id===NETWORKS[asset]&&
    ETH_ADDRESS.test(address||'')&&
    data.wallet_address?.toLowerCase()===address.toLowerCase()&&
    data.policy_id===policyId&&ID.test(data.policy_id||'')&&
    ID.test(data.execution_signer_id||'')&&
    data.owner_max_fee_atomic===cap&&
    ETH_ADDRESS.test(data.treasury_wallet||'')&&
    data.owner_grant_required===true&&
    data.privy_grant_attached===false&&
    data.automatic_payment_sent===false&&
    Number.isSafeInteger(data.policy_expires_unix)&&
    data.policy_expires_unix>Math.floor(Date.now()/1000);
}
