import React,{useEffect,useState} from 'react';
import {useSigners,useSignMessage} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {validLiveEthLimits,validSignerStatus,validatedLiveEthDraft,validWalletSignerAbsence,validEthPolicyLifecycle,validScopedGrantOffer,validEthReadOnlySafetyReport,verifiedLocalPrivyRevocationPending,usdUnits} from './liveEthRules.js';

const FIELDS=['planned_capital_usd','max_order_usd','max_daily_turnover_usd'];
const IS_ETH_ADDRESS=/^0x[a-fA-F0-9]{40}$/;

// The user may sign an explicit MiniNex policy creation intent. Wallet
// delegation requires a SEPARATE user-approved Privy addSigners call, and is
// hidden unless the server has independently verified an audited policy.
// The existing revoke button removes ALL additional signers per Privy's API.
export default function LiveEthControls({t,user,address,getAccessToken,enabled}) {
  const {removeSigners,addSigners}=useSigners();
  const {signMessage}=useSignMessage();
  const [limits,setLimits]=useState(null);
  const [grant,setGrant]=useState(null);
  const [policyState,setPolicyState]=useState(null);
  const [grantOffer,setGrantOffer]=useState(null);
  const [safetyAudit,setSafetyAudit]=useState(null);
  const [draft,setDraft]=useState(null);
  const [phase,setPhase]=useState('loading');
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  const [confirmRevoke,setConfirmRevoke]=useState(false);
  const userId=user?.id;

  const linked=(user?.linkedAccounts||[]).filter(a=>
    a?.type==='wallet' && a?.chainType==='ethereum' &&
    a?.walletClientType==='privy' &&
    typeof a.address==='string' &&
    a.address.toLowerCase()===String(address||'').toLowerCase());
  const ownAddress=IS_ETH_ADDRESS.test(address||'')&&linked.length===1?address:null;
  // A client-side delegated hint is useful only for offering a revoke.
  // Enrollment instead uses the server's authoritative Privy GET check.
  const mayHaveSigners=!!ownAddress && (linked[0]?.delegated===true || grant?.wallet_delegated===true);
  const canRequestPrivyRemoval=!!ownAddress && (mayHaveSigners||policyState?.status==='REVOKE_REQUESTED');

  const getVerifiedToken=async()=>{
    const token=await getAccessToken();
    if(!token)throw Error('Sign in required');
    return token;
  };
  const request=async(path,method='GET',body)=>{
    const token=await getVerifiedToken();
    const response=await fetch(API_BASE+path,{
      method,
      headers:{
        Authorization:'Bearer '+token,
        Accept:'application/json',
        ...(body?{'Content-Type':'application/json'}:{})
      },
      cache:'no-store',
      ...(body?{body:JSON.stringify(body)}:{})
    });
    if(!response.ok)throw Error('LIVE ETH backend unavailable');
    return response.json();
  };
  const applyLimits=data=>{
    if(!validLiveEthLimits(data))throw Error('Invalid LIVE ETH limits response');
    setLimits(data);
    setDraft({
      planned_capital_usd:data.planned_capital_usd,
      max_order_usd:data.max_order_usd,
      max_daily_turnover_usd:data.max_daily_turnover_usd,
    });
  };
  const refresh=async()=>{
    const [currentLimits,currentGrant,currentPolicy]=await Promise.all([
      request('/api/live/eth/limits'),
      request('/api/live/eth/authorization'),
      request('/api/live/eth/policy/lifecycle'),
    ]);
    if(!validLiveEthLimits(currentLimits)||!validSignerStatus(currentGrant)||
       !validEthPolicyLifecycle(currentPolicy))
      throw Error('Unverified LIVE ETH response');
    applyLimits(currentLimits);
    setGrant(currentGrant);
    setPolicyState(currentPolicy);
    if(['REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED'].includes(currentPolicy.status)){
      try{setGrantOffer(await request('/api/live/eth/policy/grant-review'));}
      catch{setGrantOffer(null);}
    }else setGrantOffer(null);
    setPhase('ready');
  };

  useEffect(()=>{
    let active=true;
    if(!enabled||!userId){
      setLimits(null);setGrant(null);setDraft(null);setPolicyState(null);setGrantOffer(null);
      setSafetyAudit(null);
      setPhase('loading');setConfirmRevoke(false);
      return()=>{active=false;};
    }
    setPhase('loading');
    (async()=>{
      try{
        const [lim,authorization,lifecycle]=await Promise.all([
          request('/api/live/eth/limits'),
          request('/api/live/eth/authorization'),
          request('/api/live/eth/policy/lifecycle'),
        ]);
        if(!validLiveEthLimits(lim)||!validSignerStatus(authorization)||
           !validEthPolicyLifecycle(lifecycle))throw Error('Unverified status');
        if(!active)return;
        applyLimits(lim);setGrant(authorization);setPolicyState(lifecycle);
        if(['REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED'].includes(lifecycle.status)){
          try{
            const offer=await request('/api/live/eth/policy/grant-review');
            if(active)setGrantOffer(offer);
          }catch{if(active)setGrantOffer(null);}
        }else setGrantOffer(null);
        setPhase('ready');
      }catch{
        if(active){setLimits(null);setGrant(null);setPhase('failed');}
      }
    })();
    return()=>{active=false;};
  },[enabled,userId,getAccessToken]);

  const save=async()=>{
    if(busy||phase!=='ready'||!limits)return;
    const payload=validatedLiveEthDraft(draft);
    if(!payload){setMessage(t.liveEthInvalidLimits);return;}
    setBusy(true);setMessage('');
    try{
      const result=await request('/api/live/eth/limits','PUT',{
        ...payload,expected_revision:limits.revision
      });
      applyLimits(result);
      setSafetyAudit(null);
      setMessage(verifiedLocalPrivyRevocationPending(result)
        ?t.liveEthLimitsSavedRevokeNeeded:t.liveEthLimitsSaved);
      try{await refresh();}catch{ /* The saved stop still remains effective. */ }
    }catch{
      setMessage(t.liveEthSaveFailed);
      setPhase('failed');
    }finally{setBusy(false);}
  };

  const stop=async()=>{
    if(busy||!enabled)return;
    setBusy(true);setMessage('');
    try{
      const result=await request('/api/live/eth/stop','POST');
      applyLimits(result);
      setSafetyAudit(null);
      setMessage(verifiedLocalPrivyRevocationPending(result)
        ?t.liveEthStoppedRevokeNeeded:t.liveEthStopped);
      try{await refresh();}catch{ /* Never claim a remote removal. */ }
    }catch{setMessage(t.liveEthStopFailed);}
    finally{setBusy(false);}
  };

  const revoke=async()=>{
    if(busy||!confirmRevoke||!canRequestPrivyRemoval||!enabled)return;
    setBusy(true);setMessage('');
    let serverStopped=false;
    let walletRemovalRequested=false;
    let remotelyVerified=false;
    // The user explicitly confirms removing ALL additional signers.
    // First atomically stop LIVE sends and revoke the MiniNex signed intent
    // plus any durable per-wallet policy. A backend outage must not prevent
    // the wallet owner from using Privy's native revoke control.
    try{
      const local=await request('/api/live/eth/consent/revoke','POST');
      serverStopped=local?.mininex_live_sends_paused===true &&
        local?.local_consent_intent_revoked===true &&
        local?.execution_enabled===false;
    }catch{
      // Last-resort LOCAL stop even if consent ledger is unavailable.
      try{
        const fallback=await request('/api/live/eth/stop','POST');
        if(validLiveEthLimits(fallback)&&fallback.trading_paused===true){
          applyLimits(fallback);
          serverStopped=true;
        }
      }catch{ /* Privy removal still proceeds with user confirmation. */ }
    }
    try{
      await removeSigners({address:ownAddress});
      walletRemovalRequested=true;
    }catch{ /* Never infer all signers were removed from an SDK error. */ }
    if(walletRemovalRequested){
      try{
        const observed=await request('/api/live/eth/policy/wallet-signer-absence');
        remotelyVerified=validWalletSignerAbsence(observed,ownAddress);
        if(remotelyVerified&&serverStopped){
          // Optional per-wallet local ledger finalization. Legacy users may
          // have no staged policy record; never treat its absence as an
          // excuse to reattach signers or to claim policy deletion.
          try{await request('/api/live/eth/policy/confirm-revocation','POST');}
          catch{ /* Only independent remote signer absence is reported. */ }
        }
      }catch{ /* Remotely unverified is NOT a successful revocation. */ }
    }
    setConfirmRevoke(false);
    setMessage(remotelyVerified&&serverStopped?t.liveEthRevoked:
      remotelyVerified?t.liveEthRevokedServerFailed:
      walletRemovalRequested?t.liveEthRevocationVerificationPending:
      serverStopped?t.liveEthRevocationFailed:t.liveEthBothFailed);
    try{await refresh();}catch{setPhase('failed');}
    finally{setBusy(false);}
  };

  const setupPolicy=async()=>{
    if(busy||phase!=='ready'||!ownAddress||!limits?.trading_paused||
       !policyState||!['NOT_PREPARED','PREPARED'].includes(policyState.status)||
       usdUnits(limits.max_order_usd)<=0n)return;
    setBusy(true);setMessage('');
    try{
      if(policyState.status==='NOT_PREPARED'){
        // This EIP-191 signature records enrollment intent ONLY. It does
        // not delegate a wallet or authorize an on-chain transaction.
        const challenge=await request('/api/live/eth/consent/challenge','POST');
        if(challenge?.wallet?.toLowerCase()!==ownAddress.toLowerCase()||
           challenge?.execution_enabled!==false||typeof challenge?.challenge!=='string')
          throw Error('Unverified consent challenge');
        const signed=await signMessage({message:challenge.challenge},{
          address:ownAddress
        });
        if(typeof signed?.signature!=='string')throw Error('Wallet signature missing');
        const confirmed=await request('/api/live/eth/consent/confirm','POST',{
          signature:signed.signature
        });
        if(confirmed?.execution_enabled!==false||confirmed?.status!=='SIGNED_INTENT')
          throw Error('Unsigned enrollment intent');
        const prepared=await request('/api/live/eth/policy/prepare','POST');
        if(prepared?.status!=='PREPARED'||prepared?.execution_enabled!==false)
          throw Error('Policy preparation not verified');
      }
      // This POST can create one non-delegated Privy policy after a DB
      // SUBMITTING reservation. An uncertain result may NEVER auto-retry.
      const published=await request('/api/live/eth/policy/create','POST');
      if(published?.status!=='REMOTE_VERIFIED_UNATTACHED'||
         published?.execution_enabled!==false)throw Error('Policy readback missing');
      setMessage(t.liveEthPolicyCreated);
    }catch{
      setMessage(t.liveEthPolicySetupFailed);
    }finally{
      try{await refresh();}catch{setPhase('failed');}
      setBusy(false);
    }
  };

  const grantPrivySigner=async()=>{
    if(busy||!ownAddress||!validScopedGrantOffer(grantOffer,ownAddress)||
       !limits?.trading_paused)return;
    setBusy(true);setMessage('');
    try{
      const offer=await request('/api/live/eth/policy/grant-review');
      if(!validScopedGrantOffer(offer,ownAddress)||
         offer.policy_id!==grantOffer.policy_id||
         offer.execution_signer_id!==grantOffer.execution_signer_id)
        throw Error('Privy grant terms changed');
      // THE USER must explicitly accept Privy's native owner authorization.
      // No empty policyIds, alternate contracts or broad wallet delegates.
      await addSigners({
        address:ownAddress,
        signers:[{signerId:offer.execution_signer_id,policyIds:[offer.policy_id]}]
      });
      const checked=await request('/api/live/eth/policy/confirm-grant','POST');
      if(checked?.status!=='GRANT_VERIFIED'||
         checked?.signer_policy_exactly_verified!==true||
         checked?.execution_enabled!==false)
        throw Error('Privy signer grant not independently verified');
      setMessage(t.liveEthGrantVerified);
    }catch{
      setMessage(t.liveEthGrantVerificationFailed);
    }finally{
      try{await refresh();}catch{setPhase('failed');}
      setBusy(false);
    }
  };

  const reviewSafety=async()=>{
    if(busy||phase!=='ready'||!enabled)return;
    setBusy(true);setMessage('');
    try{
      const audit=await request('/api/live/eth/prelaunch-safety');
      if(!validEthReadOnlySafetyReport(audit))throw Error('Invalid read-only ETH audit');
      setSafetyAudit(audit);
    }catch{
      setSafetyAudit(null);
      setMessage(t.liveEthAuditFailed);
    }finally{setBusy(false);}
  };

  const changes=FIELDS.some(f=>draft?.[f]!==limits?.[f]);
  const canSave=phase==='ready'&&!busy&&!!validatedLiveEthDraft(draft)&&changes;

  return <details className="privy-disclosure privy-technical-section">
    <summary>
      <span>{t.liveEthTitle}</span>
      <small>{t.liveEthHint}</small>
    </summary>
    <div className="privy-disclosure-body" style={{display:'grid',gap:12}}>
      <p className="privy-wallet-description">{t.liveEthSafety}</p>
      {phase==='loading'&&<p className="privy-wallet-description">{t.liveEthLoading}</p>}
      {phase==='failed'&&<p className="privy-wallet-description" role="alert">{t.liveEthUnavailable}</p>}
      {phase==='ready'&&limits&&grant&&<>
        <div className="privy-hwm-box">
          <strong>{t.liveEthGrantTitle}</strong>
          <p className="privy-wallet-description">
            {grant.signer_verified&&grant.policy_reference_verified
              ?t.liveEthGrantFound:grant.wallet_delegated
              ?t.liveEthOtherSigners:t.liveEthNoGrant}
          </p>
          <p className="privy-wallet-description">{t.liveEthGrantNotReady}</p>
        </div>
        <div className="privy-hwm-box" style={{display:'grid',gap:9}}>
          <strong>{t.liveEthLimitsTitle}</strong>
          {FIELDS.map((field,i)=><label key={field} style={{display:'grid',gap:4}}>
            <span className="privy-wallet-description">{[
              t.liveEthCapital,t.liveEthPerOrder,t.liveEthDaily
            ][i]} (USD)</span>
            <input type="text" inputMode="decimal" autoComplete="off"
              value={draft?.[field]||''}
              disabled={busy}
              onChange={e=>setDraft(current=>({...current,[field]:e.target.value}))}
              aria-label={[t.liveEthCapital,t.liveEthPerOrder,t.liveEthDaily][i]}
              style={{padding:'9px 12px',borderRadius:9,border:'1px solid var(--border-color, #475569)',background:'transparent',color:'inherit',width:'100%',boxSizing:'border-box'}}/>
          </label>)}
          <p className="privy-wallet-description">{t.liveEthLimitsSafety}</p>
          <button className="privy-wallet-action privy-wallet-action--secondary"
            type="button" onClick={save} disabled={!canSave}>{t.liveEthSaveLimits}</button>
          {changes&&!validatedLiveEthDraft(draft)&&
            <p role="alert" className="privy-wallet-description">{t.liveEthInvalidLimits}</p>}
        </div>
        <div className="privy-hwm-box" style={{display:'grid',gap:8}}>
          <strong>{t.liveEthPolicyTitle}</strong>
          <p className="privy-wallet-description">
            {policyState?.status==='REMOTE_VERIFIED_UNATTACHED'
              ?t.liveEthPolicyCreated:policyState?.status==='GRANT_VERIFIED'
              ?t.liveEthGrantVerified:policyState?.status==='PREPARED'
              ?t.liveEthPolicyPrepared:policyState?.status==='NOT_PREPARED'
              ?t.liveEthPolicyNotPrepared:t.liveEthPolicyNeedsReview}
          </p>
          {['NOT_PREPARED','PREPARED'].includes(policyState?.status)&&
            <button className="privy-wallet-action privy-wallet-action--secondary"
              type="button" onClick={setupPolicy}
              disabled={busy||!ownAddress||!limits.trading_paused||
                usdUnits(limits.max_order_usd)===0n||changes}>
              {t.liveEthPolicySetupButton}</button>}
          {validScopedGrantOffer(grantOffer,ownAddress)&&
            <>
              <p role="alert" className="privy-wallet-description">
                {t.liveEthPrivyGrantWarning}
              </p>
              <button type="button" className="privy-wallet-action"
                disabled={busy||changes||!limits.trading_paused}
                onClick={grantPrivySigner}>{t.liveEthGrantButton}</button>
            </>}
          {!validScopedGrantOffer(grantOffer,ownAddress)&&
             policyState?.status==='REMOTE_VERIFIED_UNATTACHED'&&
            <p className="privy-wallet-description">{t.liveEthGrantAuditLocked}</p>}
        </div>
        <div className="privy-hwm-box" style={{display:'grid',gap:8}}>
          <strong>{t.liveEthAuditTitle}</strong>
          <p className="privy-wallet-description">{t.liveEthAuditIntro}</p>
          <button className="privy-wallet-action privy-wallet-action--secondary"
            type="button" disabled={busy} onClick={reviewSafety}>
            {t.liveEthAuditButton}</button>
          {safetyAudit&&<div style={{display:'grid',gap:4}}>
            <p role="status" className="privy-wallet-description">
              {t.liveEthAuditBlocked} ({safetyAudit.blockers.length})
            </p>
            <p className="privy-wallet-description">
              {t.liveEthAuditPending}: {safetyAudit.live_ledger.pending_reserved_usd} USD
              {' · '}{t.liveEthAuditConfirmed}: {safetyAudit.live_ledger.confirmed_24h_usd} USD
              {' · '}{t.liveEthAuditRemaining}: {safetyAudit.live_ledger.available_24h_after_pending_usd} USD
            </p>
            {safetyAudit.manual_policy_reconciliation_required&&
              <p role="alert" className="privy-wallet-description">
                {t.liveEthAuditManualReview}</p>}
            {safetyAudit.policy_expired&&
              <p role="alert" className="privy-wallet-description">
                {t.liveEthAuditPolicyExpired}</p>}
            {safetyAudit.live_ledger.unresolved_order_count>0&&
              <p role="alert" className="privy-wallet-description">
                {t.liveEthAuditUnresolved}: {safetyAudit.live_ledger.unresolved_order_count}
              </p>}
            <p className="privy-wallet-description">{t.liveEthAuditNoTrades}</p>
          </div>}
        </div>
        <button className="privy-wallet-action privy-wallet-action--secondary"
          type="button" onClick={stop} disabled={busy}>{t.liveEthStopButton}</button>
        {canRequestPrivyRemoval&&<div className="privy-hwm-box" style={{display:'grid',gap:8}}>
          <strong>{t.liveEthRevokeTitle}</strong>
          <p className="privy-wallet-description">{t.liveEthRevokeAllWarning}</p>
          {!confirmRevoke
            ?<button className="privy-wallet-action privy-wallet-action--secondary"
              type="button" onClick={()=>setConfirmRevoke(true)} disabled={busy}>
              {t.liveEthRevokeButton}</button>
            :<>
              <p className="privy-wallet-description" role="alert">{t.liveEthRevokeConfirm}</p>
              <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
                <button className="privy-wallet-action" type="button" onClick={revoke}
                  disabled={busy}>{t.liveEthConfirmButton}</button>
                <button className="privy-wallet-action privy-wallet-action--secondary"
                  type="button" disabled={busy}
                  onClick={()=>setConfirmRevoke(false)}>{t.liveEthCancel}</button>
              </div>
            </>}
        </div>}
      </>}
      {message&&<p className="privy-wallet-description" role="status">{message}</p>}
      {enabled&&<button className="privy-wallet-action privy-wallet-action--secondary"
        type="button" disabled={busy}
        onClick={()=>{setConfirmRevoke(false);setMessage('');setPhase('loading');
          refresh().catch(()=>setPhase('failed'));}}>{t.liveEthRefresh}</button>}
    </div>
  </details>;
}
