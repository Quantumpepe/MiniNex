// Dedicated LIVE ETH controls. Never derive a LIVE authorization from PAPER settings.
const AMOUNT=/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$/;
const MAX=10000000n*1000000n;

export function usdUnits(value) {
  if(typeof value!=='string'||value.length>48||!AMOUNT.test(value))return null;
  const [whole,fraction='']=value.split('.');
  try {
    const n=BigInt(whole)*1000000n+BigInt(fraction.padEnd(6,'0')||'0');
    return n>=0n && n<MAX?n:null;
  }catch{return null;}
}

export function validLiveEthLimits(snapshot) {
  if(snapshot?.asset!=='ETH'||snapshot?.chain_id!==1||
     snapshot?.mode!=='LIVE_LIMITS_PREPARATION_ONLY'||
     snapshot?.live_trading_enabled!==false||
     snapshot?.automated_signing_enabled!==false||
     snapshot?.privy_grant_verified!==false||
     typeof snapshot?.trading_paused!=='boolean'||
     !Number.isSafeInteger(snapshot?.revision)||snapshot.revision<0)return false;
  const cap=usdUnits(snapshot.planned_capital_usd);
  const each=usdUnits(snapshot.max_order_usd);
  const day=usdUnits(snapshot.max_daily_turnover_usd);
  if(cap===null||each===null||day===null)return false;
  return (cap===0n&&each===0n&&day===0n) ||
    (cap>0n && each>0n && day>0n && each<=cap && each<=day);
}

export function validSignerStatus(snapshot) {
  return snapshot?.mode==='PRIVY_SIGNER_VERIFICATION_ONLY' &&
    snapshot?.asset==='ETH' && snapshot?.chain_id===1 &&
    snapshot?.execution_enabled===false &&
    snapshot?.automated_signing_enabled===false &&
    snapshot?.trading_approved===false &&
    snapshot?.policy_contents_verified===false &&
    typeof snapshot?.wallet_delegated==='boolean' &&
    typeof snapshot?.signer_verified==='boolean' &&
    typeof snapshot?.policy_reference_verified==='boolean';
}

export function validatedLiveEthDraft(draft) {
  if(!draft)return null;
  const cap=usdUnits(draft.planned_capital_usd);
  const order=usdUnits(draft.max_order_usd);
  const daily=usdUnits(draft.max_daily_turnover_usd);
  if(cap===null||order===null||daily===null)return null;
  if((cap===0n && order===0n && daily===0n) ||
     (cap>0n && order>0n && daily>0n && order<=cap && order<=daily)){
    return {
      planned_capital_usd:draft.planned_capital_usd,
      max_order_usd:draft.max_order_usd,
      max_daily_turnover_usd:draft.max_daily_turnover_usd
    };
  }
  return null;
}
