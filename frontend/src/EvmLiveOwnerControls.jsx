import React,{useEffect,useState} from 'react';
import {useSignMessage,useSigners} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {
  LIVE_EVM_ASSETS,validEvmTradeChallenge,validEvmTradeSignerOffer,
  validEvmTradePermission,validEvmAutoSession,
} from './evmLiveOwnerRules.js';

const WORDS={
  de:{
    title:'LIVE Handel',open:'LIVE Handel einrichten',
    desc:'Spot-Handel mit deinem eigenen Coin-Budget. Keine künstliche tägliche Handelsgrenze. Jede Wallet-Berechtigung wird getrennt und ausdrücklich bestätigt.',
    status:'LIVE-Sitzung',setup:'Handel mit Privy verbinden',
    grant:'Privy-Handelsberechtigung freigeben',
    start:'LIVE starten',pause:'Handel pausieren',
    warn:'Eine bestätigte Privy-Signatur erlaubt nach ausdrücklichem LIVE-Start echte Blockchain-Transaktionen. Netzwerkgebühren und Handelsverluste sind möglich. Noch kein Auftrag wird durch diesen Freigabe-Klick gesendet.',
    needBudget:'Coin-Budget und Stablecoin in den Handelseinstellungen speichern, bevor du startest.',
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
    title:'LIVE trading',open:'Set up LIVE trading',
    desc:'Spot trading within your own per-coin budget. No artificial daily trading cap. Every wallet permission requires a separate explicit approval.',
    status:'LIVE session',setup:'Connect trading with Privy',
    grant:'Approve Privy trading signer',
    start:'Start LIVE',pause:'Pause trading',
    warn:'A verified Privy grant authorizes real blockchain transactions only after you explicitly start LIVE. Network fees and losses are possible. This grant click does not send a trade.',
    needBudget:'Save your coin budget and settlement stablecoin before enrolling.',
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
  const {signMessage}=useSignMessage();
  const {addSigners}=useSigners();
  const t=WORDS[lang==='de'?'de':'en'];
  const [open,setOpen]=useState(false);
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
    setOpen(false);setPermission(null);setSession(null);setBusy(false);setError('');
    if(!allowed)return()=>{active=false;};
    (async()=>{
      try{
        const [p,s]=await Promise.all([
          request(path+'/permission-status/'+asset),
          request(path+'/auto-session/'+asset),
        ]);
        if(!validEvmTradePermission(p,asset)||!validEvmAutoSession(s,asset))
          throw Error('Invalid backend trading session state');
        if(active){setPermission(p);setSession(s);}
      }catch{if(active)setError(t.failed);}
    })();
    return()=>{active=false;};
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
  const setup=()=>command(async()=>{
    if(permission?.status!=='NOT_PREPARED')return;
    const challenge=await request(path+'/consent/'+asset+'/challenge','POST');
    if(!validEvmTradeChallenge(challenge,{asset,address}))
      throw Error('Original coin scope differs from your wallet');
    const signed=await signMessage({message:challenge.challenge},{address});
    if(!/^0x[0-9a-fA-F]{130}$/.test(signed?.signature||''))
      throw Error('Wallet consent signature missing');
    const consent=await request(path+'/consent/'+asset+'/confirm','POST',{
      signature:signed.signature,
    });
    if(consent?.state!=='SIGNED_INTENT'||consent?.owner_signature_verified!==true||
       consent?.additional_privy_grant_still_required!==true||
       consent?.live_trading_enabled!==false)
      throw Error('Owner-signed coin budget was not independently accepted');
    const draft=await request(path+'/policy/'+asset+'/prepare','POST');
    if(draft?.mode!=='OWNER_SIGNED_EVM_PRIVY_POLICY_DRAFT_ONLY'||
       draft?.asset!==asset||
       draft?.signed_budget_revision!==challenge.budget_revision||
       draft?.owner_delegation_granted!==false||
       draft?.policy_published_in_privy!==false)
      throw Error('Unverified source coin policy');
    // This user action prepares & publishes a policy, but NEVER delegates.
    const published=await request(path+'/policy/'+asset+'/publish','POST');
    if(published?.status!=='REMOTE_VERIFIED_UNATTACHED'||
       published?.asset!==asset||!published?.policy_id||
       published?.wallet_signer_attached!==false||
       published?.separate_privy_owner_add_signer_required!==true)
      throw Error('Trading policy publication outcome needs reconciliation');
  });
  const publishPrepared=()=>command(async()=>{
    if(permission?.status!=='DRAFT')return;
    const published=await request(path+'/policy/'+asset+'/publish','POST');
    if(published?.status!=='REMOTE_VERIFIED_UNATTACHED'||
       published?.asset!==asset||
       published?.wallet_signer_attached!==false||
       published?.separate_privy_owner_add_signer_required!==true)
      throw Error('Original published policy requires manual reconciliation');
  });
  const grant=()=>command(async()=>{
    if(permission?.status!=='CONSENT_VERIFIED')return;
    const offer=await request(path+'/policy/'+asset+'/grant-review');
    if(!validEvmTradeSignerOffer(offer,{
      asset,stable,address,policyId:offer?.policy_id,
    }))throw Error('Privy owner trading grant is not exact');
    // Only a real click in the user's Privy wallet may authorize the signer.
    await addSigners({
      address,signers:[{
        signerId:offer.execution_signer_id,
        policyIds:[offer.policy_id],
      }],
    });
    const confirmed=await request(path+'/policy/'+asset+'/confirm-grant','POST');
    if(confirmed?.state!=='GRANT_VERIFIED'||
       confirmed?.owner_signer_grant_verified!==true||
       confirmed?.transaction_signed_or_sent!==false)
      throw Error('Owner trading signer grant not verified by MiniNex');
  });
  const start=()=>command(async()=>{
    if(permission?.status!=='GRANT_VERIFIED'||!permission.signer_grant_recorded)
      throw Error('Original owner signer grant required');
    const result=await request(path+'/auto-session/'+asset+'/start','POST');
    if(!validEvmAutoSession(result,asset)||result.status!=='ACTIVE'||
       result.started_by_owner!==true)
      throw Error('User-started LIVE session not verified');
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
    <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      onClick={()=>setOpen(value=>!value)} aria-expanded={open}>{t.open} · {asset}</button>
    {open&&<>
      <strong>{t.title} · {asset}</strong>
      <p className="privy-wallet-description">{t.desc}</p>
      <p className="privy-wallet-description" role="status">
        {t.status}: {sessionStatus==='ACTIVE'?t.running:
          sessionStatus==='PAUSED'?t.paused:
          current==='CONSENT_VERIFIED'?t.unattached:
          current==='GRANT_VERIFIED'?t.ready:
          current==='REVOKED'?t.revoked:t.idle}
      </p>
      {current==='NOT_PREPARED'&&<>
        <p className="privy-wallet-description">{t.needBudget}</p>
        <button className="privy-wallet-action" type="button"
          disabled={busy} onClick={setup}>{t.setup}</button>
      </>}
      {current==='DRAFT'&&
        <button className="privy-wallet-action" type="button" disabled={busy}
          onClick={publishPrepared}>{t.publish}</button>}
      {current==='CONSENT_VERIFIED'&&<>
        <p className="privy-wallet-description" role="alert">{t.warn}</p>
        <button className="privy-wallet-action" type="button"
          disabled={busy} onClick={grant}>{t.grant}</button>
      </>}
      {current==='GRANT_VERIFIED'&&sessionStatus!=='ACTIVE'&&
        <button type="button" className="privy-wallet-action"
          disabled={busy||!permission?.signer_grant_recorded}
          onClick={start}>{t.start}</button>}
      {sessionStatus==='ACTIVE'&&
        <button type="button" className="privy-wallet-action"
          disabled={busy} onClick={pause}>{t.pause}</button>}
      {sessionStatus==='ACTIVE'&&
        <p className="privy-wallet-description">{t.off}</p>}
      <p className="privy-wallet-description">{t.note}</p>
      {busy&&<p className="privy-wallet-description" role="status">{t.signing}</p>}
      {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
    </>}
  </section>;
}
