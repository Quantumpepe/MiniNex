import React,{useEffect,useState} from 'react';
import {useSigners,useSignMessage} from '@privy-io/react-auth';
import EthLiveWalletEquity from './EthLiveWalletEquity.jsx';
import {
  authorizeCentralEvmWalletCoin,validCentralWalletStatus,
  centralWalletHasCoin,
} from './centralEvmWalletFlow.js';
import EthAutoOwnerApproval from './EthAutoOwnerApproval.jsx';
import {API_BASE} from './data.js';
import {validLiveEthLimits,validSignerStatus,validatedLiveEthDraft,validWalletSignerAbsence,validEthPolicyLifecycle,validScopedGrantOffer,validEthReadOnlySafetyReport,validEthPrivyIncidentReview,verifiedLocalPrivyRevocationPending,usdUnits} from './liveEthRules.js';

const FIELDS=['planned_capital_usd'];
const IS_ETH_ADDRESS=/^0x[a-fA-F0-9]{40}$/;

// The user may sign an explicit MiniNex policy creation intent. Wallet
// delegation requires a SEPARATE user-approved Privy addSigners call, and is
// hidden unless the server has independently verified an audited policy.
// The existing revoke button removes ALL additional signers per Privy's API.
export default function LiveEthControls({t,user,address,getAccessToken,enabled,compact=false,stable='USDC',lang='de'}) {
  const {removeSigners,addSigners}=useSigners();
  const {signMessage}=useSignMessage();
  const [limits,setLimits]=useState(null);
  const [grant,setGrant]=useState(null);
  const [ethAuto,setEthAuto]=useState(null);
  const [policyState,setPolicyState]=useState(null);
  const [grantOffer,setGrantOffer]=useState(null);
  const [safetyAudit,setSafetyAudit]=useState(null);
  const [incidentReview,setIncidentReview]=useState(null);
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
  // One original ETH wallet and signer for all selected Ethereum-network coins.
  // Owner authorization is never presented as a completed blockchain trade.
  const readEthAuto=async()=>{
    const [legacy,shared]=await Promise.all([
      request('/api/live/eth/auto/session'),
      request('/api/live/evm/auto-session/ETH'),
    ]);
    if(shared?.mode!=='OWNER_EVM_LIVE_SESSION_CONTROL'||
       shared.asset!=='ETH'||!['NOT_STARTED','PAUSED','ACTIVE'].includes(shared.status))
      throw Error('Original wallet ETH LIVE session not verified');
    return shared.status==='ACTIVE'
      ? {...legacy,status:'ACTIVE',stable:shared.stable,
         central:true,worker_deployed:shared.worker_attached===true,
         shared_submitted:shared.original_blockchain_swaps_sent||0}
      : {...legacy,central:false};
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
    try{setEthAuto(await readEthAuto());}
    catch{setEthAuto(null);}
    setPhase('ready');
  };

  useEffect(()=>{
    let active=true;
    if(!enabled||!userId){
      setLimits(null);setGrant(null);setDraft(null);setPolicyState(null);setGrantOffer(null);
      setSafetyAudit(null);setIncidentReview(null);
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
        try{
          const state=await readEthAuto();
          if(active)setEthAuto(state);
        }catch{if(active)setEthAuto(null);}
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
      setSafetyAudit(null);setIncidentReview(null);
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
      setSafetyAudit(null);setIncidentReview(null);
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
        if(remotelyVerified){
          // One original combined signer may cover ETH and all other EVM
          // assets. Revoke its wallet-wide ledger only after remote
          // Privy signer absence, never by trusting an SDK success alone.
          try{
            const aggregate=await request(
              '/api/live/evm/wallet-policy/confirm-revocation','POST',
            );
            if(!['REVOKED','NOT_CONFIGURED'].includes(aggregate?.status)||
               aggregate.wallet_signer_absence_verified!==true){
              remotelyVerified=false;
            }
          }catch{
            // The wallet itself may be safe, but the local grant needs
            // reconciliation before an ETH revocation can be reported.
            remotelyVerified=false;
          }
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

  const startEthAuto=async()=>{
    if(busy||ethAuto?.status==='ACTIVE'||!limits?.trading_paused||
       policyState?.status!=='GRANT_VERIFIED'||!ownAddress)return;
    setBusy(true);setMessage('');
    try{
      const result=await request('/api/live/eth/auto/start','POST',{
        stable:compact?stable:(ethAuto?.stable||'USDC')});
      if(result?.status!=='ACTIVE')throw Error('start');
      setMessage(t.liveEthAutoStarted);
      await refresh();
    }catch{setMessage(t.liveEthAutoStartFailed);}
    finally{setBusy(false);}
  };
  // One Start Trade owner action. The existing server still requires its
  // own EIP-191 consent and then an explicit Privy scoped-signer grant.
  // These are separate SDK authorizations until the server-side consent
  // model is consolidated. Do not make any grant silently on login.
  const startTradeOnce=async()=>{
    if(busy||phase!=='ready'||!ownAddress||!limits?.trading_paused||
       ethAuto?.status==='ACTIVE')return;
    const payload=validatedLiveEthDraft(draft);
    if(!payload||usdUnits(payload.planned_capital_usd)<=0n){
      setMessage(t.liveEthInvalidLimits);
      return;
    }
    setBusy(true);setMessage('');
    try{
      let stored=limits;
      if(changes){
        const revised=await request('/api/live/eth/limits','PUT',{
          ...payload,expected_revision:limits.revision,
        });
        if(!validLiveEthLimits(revised)||
           revised.planned_capital_usd!==payload.planned_capital_usd)
          throw Error('ETH capital revision was not saved');
        applyLimits(revised);
        stored=revised;
      }
      if(!stored.trading_paused)throw Error('ETH trading must start from pause');
      // A single combined Privy signer must also work with ETH. Choose it
      // for a new original wallet or an already-authorized central wallet,
      // never silently replacing an existing ETH legacy grant.
      const [central,lifecycle]=await Promise.all([
        request('/api/live/evm/wallet-policy/status'),
        request('/api/live/eth/policy/lifecycle'),
      ]);
      if(!validCentralWalletStatus(central)||
         !validEthPolicyLifecycle(lifecycle))
        throw Error('Original ETH wallet authorization cannot be verified');
      if(central.status!=='NOT_CONFIGURED'||
         lifecycle.status==='NOT_PREPARED'){
        const shared=await authorizeCentralEvmWalletCoin({
          asset:'ETH',stable:compact?stable:(ethAuto?.stable||stable),
          address:ownAddress,request,addSigners,signMessage,
        });
        if(!centralWalletHasCoin(shared,'ETH'))
          throw Error('Central ETH policy was not independently verified');
        const started=await request(
          '/api/live/evm/wallet-policy/auto-session/ETH/start','POST',
        );
        if(started?.mode!=='OWNER_EVM_LIVE_SESSION_CONTROL'||
           started.asset!=='ETH'||started.status!=='ACTIVE'||
           started.started_by_owner!==true)
          throw Error('Original ETH LIVE session not activated');
        setMessage(t.liveEthAutoStarted);
        return;
      }
      if(!ethAuto?.worker_deployed)
        throw Error('Original ETH LIVE worker is not available');
      let stage=lifecycle.status;
      if(stage==='NOT_PREPARED'){
        // Restricted policy preparation is NOT wallet authorization.
        // The only user-facing approval in this path is the owner
        // Privy addSigners prompt after the policy is published.
        const prepared=await request(
          '/api/live/eth/policy/prepare-grant','POST');
        if(prepared?.status!=='PREPARED'||
           prepared?.execution_enabled!==false)
          throw Error('ETH original wallet policy could not be prepared');
        stage='PREPARED';
      }
      if(stage==='PREPARED'){
        // Never retry an ambiguous policy creation; the server owns
        // the once-only reservation and remote policy reconciliation.
        const published=await request('/api/live/eth/policy/create','POST');
        if(published?.status!=='REMOTE_VERIFIED_UNATTACHED'||
           published?.execution_enabled!==false)
          throw Error('ETH policy publication needs reconciliation');
        stage='REMOTE_VERIFIED_UNATTACHED';
      }
      if(stage==='REMOTE_VERIFIED_UNATTACHED'){
        const offer=await request('/api/live/eth/policy/grant-review');
        if(!validScopedGrantOffer(offer,ownAddress))
          throw Error('ETH Privy authorization scope changed');
        await addSigners({
          address:ownAddress,
          signers:[{
            signerId:offer.execution_signer_id,
            policyIds:[offer.policy_id],
          }],
        });
        const verified=await request('/api/live/eth/policy/confirm-grant','POST');
        if(verified?.status!=='GRANT_VERIFIED'||
           verified?.signer_policy_exactly_verified!==true||
           verified?.execution_enabled!==false)
          throw Error('ETH Privy grant not independently verified');
        stage='GRANT_VERIFIED';
      }
      if(stage!=='GRANT_VERIFIED')
        throw Error('ETH trading authorization cannot yet start');
      const current=await request('/api/live/eth/policy/lifecycle');
      if(!validEthPolicyLifecycle(current)||current.status!=='GRANT_VERIFIED')
        throw Error('ETH Privy authorization changed before start');
      const result=await request('/api/live/eth/auto/start','POST',{
        stable:compact?stable:(ethAuto?.stable||'USDC'),
      });
      if(result?.status!=='ACTIVE')throw Error('ETH LIVE session was not started');
      setMessage(t.liveEthAutoStarted);
    }catch{
      setMessage(t.liveEthAutoStartFailed);
    }finally{
      try{await refresh();}catch{setPhase('failed');}
      setBusy(false);
    }
  };

  const pauseEthAuto=async()=>{
    if(busy||ethAuto?.status!=='ACTIVE')return;
    setBusy(true);setMessage('');
    try{
      const result=await request(
        ethAuto?.central===true
          ? '/api/live/evm/auto-session/ETH/pause'
          : '/api/live/eth/auto/pause','POST');
      if(result?.status!=='PAUSED'||
         (ethAuto?.central!==true && result.future_auto_orders_blocked!==true))
        throw Error('pause');
      setMessage(t.liveEthAutoPaused);
      await refresh();
    }catch{setMessage(t.liveEthAutoPauseFailed);}
    finally{setBusy(false);}
  };

  const reviewSafety=async()=>{
    if(busy||phase!=='ready'||!enabled)return;
    setBusy(true);setMessage('');
    try{
      const audit=await request('/api/live/eth/prelaunch-safety');
      if(!validEthReadOnlySafetyReport(audit))throw Error('Invalid read-only ETH audit');
      setSafetyAudit(audit);
      // A second independent GET does not change a Privy policy, retry a
      // previously uncertain POST, or remove wallet permissions.
      try{
        const incident=await request('/api/live/eth/policy/incident-review');
        if(!validEthPrivyIncidentReview(incident))
          throw Error('Unverified read-only incident report');
        setIncidentReview(incident);
      }catch{setIncidentReview(null);}
    }catch{
      setSafetyAudit(null);setIncidentReview(null);setIncidentReview(null);
      setMessage(t.liveEthAuditFailed);
    }finally{setBusy(false);}
  };

  const changes=FIELDS.some(f=>draft?.[f]!==limits?.[f]);
  const canSave=phase==='ready'&&!busy&&!!validatedLiveEthDraft(draft)&&changes;

  // Compact variant is the single user-facing ETH trading entry point.
  // Capital is saved automatically on Start Trade; the technical Privy
  // policy stages remain internal and are not separate user buttons.
  if(compact)return <div className="privy-paper-action" aria-label="ETH LIVE AUTO">
    {phase==='loading'&&<p className="privy-wallet-description">{t.liveEthLoading}</p>}
    {phase==='failed'&&<p role="alert" className="privy-wallet-description">{t.liveEthUnavailable}</p>}
    {phase==='ready'&&limits&&<>
      <div className="privy-paper-action-top">
        <span>{t.liveEthAutoTitle}</span>
        <strong>{ethAuto?.status||'OFF'}</strong>
      </div>
      {ethAuto?.status!=='ACTIVE'&&<label style={{display:'grid',gap:4}}>
        <span className="privy-wallet-description">{t.liveEthCapital} (USD)</span>
        <input type="text" inputMode="decimal" autoComplete="off"
          value={draft?.planned_capital_usd||''} disabled={busy}
          onChange={e=>setDraft(current=>({...current,
            planned_capital_usd:e.target.value}))}
          aria-label={t.liveEthCapital}/>
      </label>}
      {ethAuto?.status==='ACTIVE'
        ?<button type="button" className="privy-wallet-action"
            disabled={busy} onClick={pauseEthAuto}>{t.liveEthAutoPause}</button>
        :<button type="button" className="privy-wallet-action"
            disabled={busy||
              !ownAddress||!limits.trading_paused||
              !validatedLiveEthDraft(draft)||
              usdUnits(draft?.planned_capital_usd)<=0n}
            onClick={startTradeOnce}>Start Trade · ETH</button>}
      {!ethAuto?.worker_deployed&&
        <p className="privy-wallet-description">{t.liveEthAutoNotDeployed}</p>}
      {message&&<p role="status" className="privy-wallet-description">{message}</p>}
    </>}
  </div>;

  return <>
    {enabled&&<div className="privy-hwm-box" style={{display:'grid',gap:8}}>
      <strong>ETH · LIVE</strong>
      <p className="privy-wallet-description">
        {t.liveEthAutoStatus}: {ethAuto?.status||'OFF'}
      </p>
      {ethAuto?.status==='ACTIVE'
        ? <button className="privy-wallet-action" type="button"
            disabled={busy} onClick={pauseEthAuto}>{t.liveEthAutoPause}</button>
        : <button className="privy-wallet-action" type="button"
            disabled={busy||phase!=='ready'||!limits||
              changes||!ownAddress||
              !limits.trading_paused}
            onClick={startTradeOnce}>Start Trade · ETH</button>}
      {!ethAuto?.worker_deployed&&
        <p className="privy-wallet-description">{t.liveEthAutoNotDeployed}</p>}
    </div>}
    <details className="privy-disclosure privy-technical-section">
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
          {FIELDS.map(field=><label key={field} style={{display:'grid',gap:4}}>
            <span className="privy-wallet-description">{t.liveEthCapital} (USD)</span>
            <input type="text" inputMode="decimal" autoComplete="off"
              value={draft?.[field]||''}
              disabled={busy}
              onChange={e=>setDraft(current=>({...current,[field]:e.target.value}))}
              aria-label={t.liveEthCapital}
              style={{padding:'9px 12px',borderRadius:9,border:'1px solid var(--border-color, #475569)',background:'transparent',color:'inherit',width:'100%',boxSizing:'border-box'}}/>
          </label>)}
          <p className="privy-wallet-description">{t.liveEthLimitsSafety}</p>
          <button className="privy-wallet-action privy-wallet-action--secondary"
            type="button" onClick={save} disabled={!canSave}>{t.liveEthSaveLimits}</button>
          {changes&&!validatedLiveEthDraft(draft)&&
            <p role="alert" className="privy-wallet-description">{t.liveEthInvalidLimits}</p>}
        </div>
        <div className="privy-hwm-box" style={{display:'grid',gap:8}}>
          <strong>{t.liveEthAutoTitle}</strong>
          <p className="privy-wallet-description">{t.liveEthAutoDescription}</p>
          <p className="privy-wallet-description">
            {t.liveEthAutoStatus}: {ethAuto?.status||'OFF'}
          </p>

        </div>
        <EthAutoOwnerApproval
          userId={userId} walletAddress={ownAddress}
          getAccessToken={getAccessToken} paused={limits?.trading_paused===true}
          enabled={enabled} t={t}/>
        <EthLiveWalletEquity
          walletAddress={ownAddress} userId={userId}
          getAccessToken={getAccessToken} enabled={enabled} t={t}/>
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
            <div className="privy-wallet-description" style={{display:'grid',gap:4}}>
              <strong>{t.liveEthIncidentTitle}</strong>
              {incidentReview
                ?<>
                  <span>{t.liveEthIncidentActions?.[incidentReview.action_code]
                    ||t.liveEthIncidentUnknown}</span>
                  {incidentReview.status==='SUBMITTING'||
                    incidentReview.status==='UNCERTAIN'
                    ?<span role="alert">{t.liveEthIncidentNeverRetry}</span>
                    :null}
                  {!incidentReview.privy_wallet_signers_verified_now&&
                    ['GRANT_VERIFIED','REVOKE_REQUESTED','REVOKED'].includes(incidentReview.status)
                    ?<span role="alert">{t.liveEthIncidentPrivyUnverified}</span>
                    :null}
                  {incidentReview.status==='REVOKE_REQUESTED'&&
                     incidentReview.privy_additional_signers_absent_now&&
                    <span>{t.liveEthIncidentConfirmRevocation}</span>}
                </>
                :<span>{t.liveEthIncidentUnavailable}</span>}
              <span>{t.liveEthIncidentReadOnly}</span>
            </div>
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
  </details>
  </>;
}
