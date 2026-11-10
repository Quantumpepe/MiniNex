import React,{useEffect,useState} from 'react';
import {useSigners} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {validLiveEthLimits,validSignerStatus,validatedLiveEthDraft} from './liveEthRules.js';

const FIELDS=['planned_capital_usd','max_order_usd','max_daily_turnover_usd'];
const IS_ETH_ADDRESS=/^0x[a-fA-F0-9]{40}$/;

// This panel NEVER grants trading permission. Privy addSigners must only be
// exposed after an independently audited, bounded MiniNex signer policy exists.
// The existing revoke button removes ALL additional signers per Privy's API.
export default function LiveEthControls({t,user,address,getAccessToken,enabled}) {
  const {removeSigners}=useSigners();
  const [limits,setLimits]=useState(null);
  const [grant,setGrant]=useState(null);
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
  const mayHaveSigners=!!ownAddress && (linked[0]?.delegated===true || grant?.wallet_delegated===true);

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
    const [currentLimits,currentGrant]=await Promise.all([
      request('/api/live/eth/limits'),
      request('/api/live/eth/authorization'),
    ]);
    if(!validLiveEthLimits(currentLimits)||!validSignerStatus(currentGrant))
      throw Error('Unverified LIVE ETH response');
    applyLimits(currentLimits);
    setGrant(currentGrant);
    setPhase('ready');
  };

  useEffect(()=>{
    let active=true;
    if(!enabled||!userId){
      setLimits(null);setGrant(null);setDraft(null);
      setPhase('loading');setConfirmRevoke(false);
      return()=>{active=false;};
    }
    setPhase('loading');
    (async()=>{
      try{
        const [lim,authorization]=await Promise.all([
          request('/api/live/eth/limits'),
          request('/api/live/eth/authorization'),
        ]);
        if(!validLiveEthLimits(lim)||!validSignerStatus(authorization))throw Error('Unverified status');
        if(!active)return;
        applyLimits(lim);setGrant(authorization);setPhase('ready');
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
      setMessage(t.liveEthLimitsSaved);
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
      setMessage(t.liveEthStopped);
    }catch{setMessage(t.liveEthStopFailed);}
    finally{setBusy(false);}
  };

  const revoke=async()=>{
    if(busy||!confirmRevoke||!ownAddress||!mayHaveSigners||!enabled)return;
    setBusy(true);setMessage('');
    let serverStopped=false;
    let walletRevoked=false;
    // Always attempt server stop first, but a backend failure MUST NOT block
    // user-owned wallet revocation via the Privy SDK.
    try{
      const result=await request('/api/live/eth/stop','POST');
      applyLimits(result);
      serverStopped=true;
    }catch{ /* Privy revocation still proceeds to protect the user. */ }
    try{
      await removeSigners({address:ownAddress});
      walletRevoked=true;
    }catch{ /* Never claim all wallet permissions were revoked. */ }
    setConfirmRevoke(false);
    setMessage(walletRevoked&&serverStopped?t.liveEthRevoked:
      walletRevoked?t.liveEthRevokedServerFailed:
      serverStopped?t.liveEthRevocationFailed:t.liveEthBothFailed);
    try{await refresh();}catch{setPhase('failed');}
    finally{setBusy(false);}
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
        <button className="privy-wallet-action privy-wallet-action--secondary"
          type="button" onClick={stop} disabled={busy}>{t.liveEthStopButton}</button>
        {mayHaveSigners&&<div className="privy-hwm-box" style={{display:'grid',gap:8}}>
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
