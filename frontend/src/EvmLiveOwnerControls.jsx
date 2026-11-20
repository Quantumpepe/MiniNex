import React,{useEffect,useState} from 'react';
import {useSigners} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {
  LIVE_EVM_ASSETS,validEvmTradeChallenge,validEvmTradeSignerOffer,
  validEvmTradePermission,validEvmAutoSession,
} from './evmLiveOwnerRules.js';

const WORDS={
  de:{
    title:'LIVE Handel',open:'Start Trade',
    desc:'Coin und Kapital einstellen, dann Start Trade drücken. MiniNex erledigt die technischen Schritte im Hintergrund. Die Wallet-Freigabe ist an dein Coin-Budget gebunden.',
    status:'LIVE-Sitzung',setup:'Handel mit Privy verbinden',
    grant:'Privy-Handelsberechtigung freigeben',
    start:'LIVE starten',pause:'Handel pausieren',
    warn:'Eine Privy-Freigabe umfasst den Handel für diesen Coin und das gewählte Kapital, die nötigen begrenzten Router-Freigaben sowie 5 % Gebühr auf realisierte Nettogewinne oberhalb des High-Water-Marks an die angezeigte MiniNex-Treasury. Keine Gewinngebühr bei Verlust. Netzwerkgebühren und Verluste sind möglich.',
    needBudget:'Zuerst Kapital und Stablecoin in den Handelseinstellungen speichern.',
    ready:'Freigabe verifiziert',unattached:'Wallet-Freigabe in Privy erforderlich',
    draft:'Policy bereit zur Veröffentlichung',publish:'Vorbereitete Policy veröffentlichen',idle:'Noch nicht verbunden',
    paused:'Pausiert',running:'Sitzung aktiv – Blockchain-Ausführung nur bei aktivem Backend-Worker',
    off:'Backend-Worker ist noch nicht als aktiv bestätigt. Ein Sitzungsstatus allein ist kein realer Trade.',
    revoked:'Widerrufen; alte Berechtigung nicht automatisch wieder aktivieren',
    failed:'Aktueller Status konnte nicht verifiziert werden.',
    error:'Die Privy-/Blockchain-Freigabe ist nicht vollständig bestätigt.',
    signing:'Wallet-Freigabe wird geprüft …',
    note:'Pausieren verhindert neue Trades, schliesst aber nicht automatisch eine offene Position. Bereits gesendete Transaktionen können nicht rückgängig gemacht werden.',
  },
  en:{
    title:'LIVE trading',open:'Start Trade',
    desc:'Set your coin and capital, then tap Start Trade. MiniNex handles the technical setup. Wallet authority is scoped to the selected coin budget.',
    status:'LIVE session',setup:'Connect trading with Privy',
    grant:'Approve Privy trading signer',
    start:'Start LIVE',pause:'Pause trading',
    warn:'One Privy approval covers trading for this coin and chosen capital, finite router allowances and a 5% performance fee on realized net gains above the high-water mark paid to the disclosed MiniNex treasury. No profit fee on losses. Network fees and losses are possible.',
    needBudget:'Save your coin capital and stablecoin in trading settings first.',
    ready:'Grant verified',unattached:'Wallet grant required in Privy',
    draft:'Policy can be published',publish:'Publish prepared policy',idle:'Not connected',
    paused:'Paused',running:'Session active – blockchain execution requires an enabled backend worker',
    off:'Backend worker activation has not been confirmed. A session status does not prove a trade.',
    revoked:'Revoked; old permission cannot automatically reactivate',
    failed:'Current status could not be independently verified.',
    error:'Privy or blockchain permission not fully verified.',
    signing:'Checking wallet grant …',
    note:'Pause prevents new trades but does not automatically sell an open position. Previously sent transactions cannot be undone.',
  },
};

export default function EvmLiveOwnerControls({
  asset,stable,address,userId,getAccessToken,enabled,lang='de',
}){
  const {addSigners}=useSigners();
  const t=WORDS[lang==='de'?'de':'en'];
  const [permission,setPermission]=useState(null);
  const [session,setSession]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const allowed=enabled&&LIVE_EVM_ASSETS.includes(asset)&&
    /^0x[0-9a-fA-F]{40}$/.test(address||'')&&
    typeof getAccessToken==='function';
  const path='/api/live/evm';
  const request=async(route,method='GET',body)=>{
    const token=await getAccessToken();
    if(!token)throw Error('Privy sign-in required');
    const resp=await fetch(API_BASE+route,{
      method,cache:'no-store',
      headers:{
        Authorization:'Bearer '+token,Accept:'application/json',
        ...(body?{'Content-Type':'application/json'}:{}),
      },
      ...(body?{body:JSON.stringify(body)}:{}),
    });
    if(!resp.ok)throw Error('MiniNex LIVE EVM verification unavailable');
    return resp.json();
  };
  const refresh=async()=>{
    const [p,s]=await Promise.all([
      request(path+'/permission-status/'+asset),
      request(path+'/auto-session/'+asset),
    ]);
    if(!validEvmTradePermission(p,asset)||!validEvmAutoSession(s,asset))
      throw Error('Inconsistent EVM LIVE trading state');
    setPermission(p);setSession(s);
  };
  useEffect(()=>{
    let active=true;
    setPermission(null);setSession(null);setBusy(false);setError('');
    if(!allowed)return()=>{active=false;};
    const poll=async()=>{
      try{
        const [p,s]=await Promise.all([
          request(path+'/permission-status/'+asset),
          request(path+'/auto-session/'+asset),
        ]);
        if(!validEvmTradePermission(p,asset)||!validEvmAutoSession(s,asset))
          throw Error('Invalid backend trading session state');
        if(active){setPermission(p);setSession(s);setError('');}
      }catch{if(active)setError(t.failed);}
    };
    void poll();
    // Passive read-only refresh: the backend heartbeat and genuine
    // blockchain submissions become visible without extra user clicks.
    // This never signs, starts, approves, or retries any transaction.
    const timer=window.setInterval(()=>{void poll();},20000);
    return()=>{active=false;window.clearInterval(timer);};
  },[asset,stable,address,userId,allowed]);
  const command=async(action)=>{
    if(!allowed||busy)return;
    setBusy(true);setError('');
    try{await action();}
    catch(e){setError(e?.message||t.error);}
    finally{
      try{await refresh();}catch{setError(t.failed);}
      setBusy(false);
    }
  };
  // One owner action orchestrates the existing durable and independently
  // verified sequence. This does not bypass either of Privy's actual
  // cryptographic approvals: a future consent protocol may consolidate them.
  // Never run on login, render, retry, or a background effect.
  const startTrade=()=>command(async()=>{
    const [freshPermission,freshSession]=await Promise.all([
      request(path+'/permission-status/'+asset),
      request(path+'/auto-session/'+asset),
    ]);
    if(!validEvmTradePermission(freshPermission,asset)||
       !validEvmAutoSession(freshSession,asset))
      throw Error('Original wallet and trade session unavailable');
    if(freshSession.status==='ACTIVE')return;
    let state=freshPermission.status;
    if(state==='REVOKED')throw Error(t.revoked);
    const mode=freshPermission.authorization_method;
    let published=freshPermission.policy_published===true;
    if(state==='NOT_PREPARED'){
      // Prepare exactly the user's saved budget and router policy, but
      // without requesting an EIP-191 message signature. At this stage
      // Privy grants NO spending authority whatsoever.
      const draft=await request(path+'/policy/'+asset+'/prepare-grant','POST');
      if(draft?.mode!=='OWNER_EVM_PRIVY_GRANT_DRAFT_ONLY'||
         draft?.asset!==asset||
         draft?.owner_delegation_granted!==false||
         draft?.policy_published_in_privy!==false||
         !Number.isSafeInteger(draft?.signed_budget_revision)||
         draft.signed_budget_revision<1)
        throw Error('Original scoped coin budget policy unavailable');
      state='DRAFT';
      published=false;
    }
    if(state==='DRAFT'&&!published){
      const publishedResult=await request(path+'/policy/'+asset+'/publish','POST');
      if(publishedResult?.status!=='REMOTE_VERIFIED_UNATTACHED'||
         publishedResult?.asset!==asset||
         publishedResult?.wallet_signer_attached!==false||
         publishedResult?.separate_privy_owner_add_signer_required!==true)
        throw Error('Uncertain wallet policy publication: no repeat permitted');
      published=true;
    }
    if((state==='DRAFT'&&published)||
       state==='CONSENT_VERIFIED'){
      const offer=await request(path+'/policy/'+asset+'/grant-review');
      if(!validEvmTradeSignerOffer(offer,{
        asset,stable,address,policyId:offer?.policy_id,
      }))throw Error('Privy owner policy differs from the saved coin budget');
      // This is the ONE user-facing wallet approval. The Privy SDK shows
      // the request and the owner explicitly authorizes the exact scoped
      // signer. No prior EIP-191 signMessage is requested by this flow.
      await addSigners({
        address,signers:[{
          signerId:offer.execution_signer_id,
          policyIds:[offer.policy_id],
        }],
      });
      const checked=await request(path+'/policy/'+asset+'/confirm-grant','POST');
      if(checked?.state!=='GRANT_VERIFIED'||
         checked?.owner_signer_grant_verified!==true||
         checked?.transaction_signed_or_sent!==false)
        throw Error('Privy owner grant not independently verified');
      state='GRANT_VERIFIED';
    }
    if(state!=='GRANT_VERIFIED')
      throw Error('Trading authorization is not yet available');
    // An old UI state, stale budget or unrelated signer can never start.
    const beforeStart=await request(path+'/permission-status/'+asset);
    if(!validEvmTradePermission(beforeStart,asset)||
       beforeStart.status!=='GRANT_VERIFIED'||
       beforeStart.signer_grant_recorded!==true)
      throw Error('Trading authorization changed; no session was started');
    const result=await request(path+'/auto-session/'+asset+'/start','POST');
    if(!validEvmAutoSession(result,asset)||result.status!=='ACTIVE'||
       result.started_by_owner!==true)
      throw Error('LIVE session was not independently started');
  });
  const pause=()=>command(async()=>{
    const result=await request(path+'/auto-session/'+asset+'/pause','POST');
    if(!validEvmAutoSession(result,asset)||result.status!=='PAUSED')
      throw Error('Cannot confirm per-coin pause');
  });
  if(!allowed)return null;
  const current=permission?.status;
  const sessionStatus=session?.status;
  return <section className="privy-hwm-box" aria-label={t.title}
    style={{display:'grid',gap:8}}>
    <strong>{t.title} · {asset}</strong>
    <p className="privy-wallet-description">{t.desc}</p>
    <p className="privy-wallet-description" role="status">
      {t.status}: {sessionStatus==='ACTIVE'?
        (session?.worker_attached===true?t.running:t.off):
        sessionStatus==='PAUSED'?t.paused:
        current==='CONSENT_VERIFIED'?t.unattached:
        current==='GRANT_VERIFIED'?t.ready:
        current==='DRAFT'?t.draft:
        current==='REVOKED'?t.revoked:t.idle}
      {Number.isSafeInteger(session?.original_blockchain_swaps_sent)&&
        session.original_blockchain_swaps_sent>0&&
        <span> · {session.original_blockchain_swaps_sent} {lang==='de'?'Blockchain-Swaps gesendet':'blockchain swaps sent'}</span>}
      {Number.isSafeInteger(session?.original_blockchain_swaps_finalized)&&
        session.original_blockchain_swaps_finalized>0&&
        <span> · {session.original_blockchain_swaps_finalized} {lang==='de'?'erfolgreich finalisiert':'successfully finalized'}</span>}
    </p>
    {sessionStatus==='ACTIVE'
      ? <button type="button" className="privy-wallet-action"
          disabled={busy} onClick={pause}>{t.pause}</button>
      : current!=='REVOKED' &&
        <button type="button" className="privy-wallet-action"
          disabled={busy||permission===null} onClick={startTrade}>
          {t.open} · {asset}
        </button>}
    {sessionStatus!=='ACTIVE'&&
      <p className="privy-wallet-description">{t.needBudget}</p>}
    {sessionStatus!=='ACTIVE'&&current!=='GRANT_VERIFIED'&&
      <p className="privy-wallet-description">{t.warn}</p>}
    <p className="privy-wallet-description">{t.note}</p>
    {busy&&<p className="privy-wallet-description" role="status">{t.signing}</p>}
    {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
  </section>;
}
