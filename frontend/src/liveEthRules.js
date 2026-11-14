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


// A successful local stop or Privy SDK promise is not proof of revocation.
// The server must independently GET the *same verified wallet* and observe
// a present, empty additional_signers list; client cannot choose that wallet.
export function validWalletSignerAbsence(snapshot,address){
  return typeof address==='string' && /^0x[a-fA-F0-9]{40}$/.test(address) &&
    snapshot?.mode==='PRIVY_SIGNER_ABSENCE_REMOTE_READ_ONLY' &&
    snapshot?.asset==='ETH' && snapshot?.chain_id===1 &&
    typeof snapshot?.verified_wallet_address==='string' &&
    snapshot.verified_wallet_address.toLowerCase()===address.toLowerCase() &&
    snapshot?.wallet_identity_verified===true &&
    snapshot?.wallet_owned_by_authenticated_user===true &&
    snapshot?.remote_wallet_metadata_verified===true &&
    snapshot?.additional_signers_field_verified===true &&
    snapshot?.signer_absence_verified===true &&
    snapshot?.privy_remote_signer_removed_by_backend===false &&
    snapshot?.privy_policy_deleted===false &&
    snapshot?.execution_enabled===false &&
    snapshot?.can_sign===false && snapshot?.can_submit===false;
}


// Wallet delegation can only use a PRIVY-server verified policy and the exact
// wallet the logged-in user owns. Never trust a client-provided signer/policy.
const PRIVY_ID=/^[A-Za-z0-9_-]{6,160}$/;
const ETH_ADDRESS=/^0x[a-fA-F0-9]{40}$/;

export function validScopedGrantOffer(data,ownAddress){
  return typeof ownAddress==='string' && ETH_ADDRESS.test(ownAddress) &&
    data?.mode==='ETH_PRIVY_SCOPED_GRANT_REVIEW_ONLY' &&
    data?.asset==='ETH' && data?.chain_id===1 &&
    data?.policy_verified===true &&
    data?.wallet_owned_by_user===true &&
    data?.unexpected_signers===false &&
    data?.user_privy_grant_verified===false &&
    data?.grant_offer_enabled===true &&
    data?.can_request_user_grant===true &&
    data?.cumulative_spend_enforced_by_policy===false &&
    data?.production_trading_enabled===false &&
    data?.can_sign===false && data?.can_submit===false &&
    data?.execution_enabled===false &&
    typeof data?.verified_wallet_address==='string' &&
    ETH_ADDRESS.test(data.verified_wallet_address) &&
    data.verified_wallet_address.toLowerCase()===ownAddress.toLowerCase() &&
    typeof data?.policy_id==='string' && PRIVY_ID.test(data.policy_id) &&
    typeof data?.execution_signer_id==='string' &&
    PRIVY_ID.test(data.execution_signer_id) &&
    data.policy_id!==data.execution_signer_id;
}

export function validEthPolicyLifecycle(data){
  return data?.mode==='ETH_PRIVY_POLICY_DURABLE_PREPARATION_ONLY' &&
    data?.asset==='ETH' && data?.chain_id===1 &&
    [
      'NOT_PREPARED','PREPARED','SUBMITTING','UNCERTAIN',
      'REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED',
      'REVOKE_REQUESTED','REVOKED',
    ].includes(data.status) &&
    data?.execution_enabled===false &&
    data?.can_submit===false && data?.can_sign===false &&
    data?.policy_created_in_privy===false;
}


// Never display a stale/partially privileged server response as a
// successful prelaunch audit. It must be owned by the authenticated user
// and still assert that all signing and transaction submission are off.
export function validEthReadOnlySafetyReport(report){
  const ledger=report?.live_ledger;
  return report?.mode==='ETH_LIVE_PRELAUNCH_AUDIT_ONLY' &&
    report?.asset==='ETH' && report?.chain_id===1 &&
    report?.read_only===true &&
    report?.policy_created_by_this_check===false &&
    report?.wallet_signer_modified_by_this_check===false &&
    report?.real_trading_enabled===false &&
    report?.can_sign===false && report?.can_submit===false &&
    report?.execution_enabled===false &&
    report?.automatic_privy_policy_retry_permitted===false &&
    Array.isArray(report.blockers) && report.blockers.length>0 &&
    report.blockers.every(s=>typeof s==='string'&&s.length>0&&s.length<=90) &&
    ledger?.mode==='ETH_LIVE_LEDGER_READ_ONLY' &&
    ledger?.asset==='ETH' && ledger?.chain_id===1 &&
    ledger?.ready_for_live===false && ledger?.execution_enabled===false &&
    ledger?.paper_simulation_included===false &&
    ledger?.can_submit===false &&
    typeof ledger.pending_reserved_usd==='string' &&
    typeof ledger.confirmed_24h_usd==='string' &&
    typeof ledger.available_24h_after_pending_usd==='string' &&
    Number.isInteger(ledger.unresolved_order_count) &&
    ledger.unresolved_order_count>=0 &&
    Number.isInteger(ledger.reconciliation_required_count) &&
    ledger.reconciliation_required_count>=0;
}


// A local stop or limits edit cannot remove a signer from Privy. Surface
// the required owner action instead of misleadingly calling it complete.
export function verifiedLocalPrivyRevocationPending(snapshot){
  return validLiveEthLimits(snapshot) &&
    snapshot.trading_paused===true &&
    snapshot.privy_remote_revocation_requested===true &&
    snapshot.privy_remote_signer_removed===false &&
    snapshot.real_trading_enabled===false;
}


// A recovery review describes an incident; it must NEVER be accepted as
// authorization to create another policy, grant a signer or execute trades.
const POLICY_RECOVERY_ACTIONS=new Set([
  'NO_POLICY_PREPARED','PREPARED_REVIEW',
  'MANUAL_RECONCILIATION_REQUIRED','REMOTE_POLICY_REVIEW',
  'RECHECK_WALLET_AUTHORITY','REMOVE_OR_VERIFY_PRIVY_SIGNERS',
  'CONFIRM_LOCAL_REVOKE_AFTER_OWNER_CHECK','REVOKED_KEEP_STOPPED',
]);

export function validEthPrivyIncidentReview(snapshot){
  if(snapshot?.mode!=='ETH_PRIVY_INCIDENT_REVIEW_READ_ONLY' ||
     snapshot?.asset!=='ETH' || snapshot?.chain_id!==1 ||
     !POLICY_RECOVERY_ACTIONS.has(snapshot.action_code) ||
     ![
       'NOT_PREPARED','PREPARED','SUBMITTING','UNCERTAIN',
       'REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED',
       'REVOKE_REQUESTED','REVOKED',
     ].includes(snapshot.status) ||
     !Array.isArray(snapshot.blockers) ||
     !snapshot.blockers.includes('ETH_LIVE_EXECUTION_DISABLED') ||
     !snapshot.blockers.includes('NO_AUTOMATIC_PRIVY_RETRY') ||
     !snapshot.blockers.every(x=>typeof x==='string'&&x.length>0&&x.length<=100) ||
     typeof snapshot.local_stop_verified!=='boolean' ||
     typeof snapshot.limits_revision_matches!=='boolean' ||
     typeof snapshot.policy_expired!=='boolean' ||
     typeof snapshot.remote_policy_exactly_verified_now!=='boolean' ||
     typeof snapshot.privy_wallet_signers_verified_now!=='boolean' ||
     typeof snapshot.privy_additional_signers_absent_now!=='boolean' ||
     (snapshot.privy_additional_signers_absent_now &&
       !snapshot.privy_wallet_signers_verified_now) ||
     (snapshot.policy_id!==null &&
       (typeof snapshot.policy_id!=='string' ||
         !/^[A-Za-z0-9_-]{6,160}$/.test(snapshot.policy_id))) ||
     (snapshot.last_remote_attempt_age_seconds!==null &&
       (!Number.isSafeInteger(snapshot.last_remote_attempt_age_seconds) ||
         snapshot.last_remote_attempt_age_seconds<0)) ||
     snapshot.remote_policy_deleted!==false ||
     snapshot.remote_policy_created_by_review!==false ||
     snapshot.privy_signer_changed_by_review!==false ||
     snapshot.automatic_retry_allowed!==false ||
     snapshot.safe_to_recreate_policy!==false ||
     snapshot.real_trading_enabled!==false ||
     snapshot.can_sign!==false || snapshot.can_submit!==false ||
     snapshot.execution_enabled!==false)return false;
  return true;
}
