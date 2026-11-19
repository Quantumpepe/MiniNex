import React,{useEffect,useState} from 'react';
import {useSigners,useSignMessage} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {EVM_FEE_COINS,exactFeeCapAtomic,validFeeAutoChallenge,
  validFeeAutoGrantOffer} from './evmFeeAutoConsentRules.js';

const COPY={
  de:{
    title:'5 % Gewinngebühr · automatisch',open:'Gebühren-Einwilligung',
    intro:'Nur 5 % des nach echten Handelskosten realisierten Gewinns oberhalb der High-Water-Mark. Die Gebührenberechtigung ist getrennt vom Handel. Eine Zustimmung ist freiwillig und kann lokal widerrufen werden.',
    cap:'Maximale einzelne Gebührenzahlung',hint:'Du bestimmst die Obergrenze dieser Privy-Berechtigung selbst. Keine Handels- oder Tagesgrenze.',
    authorize:'Gebühren-Einwilligung signieren',publish:'Gebühren-Policy veröffentlichen',
    grant:'Privy-Gebührensigner erlauben',revoke:'Gebührenberechtigung widerrufen',
    confirmRevoke:'Widerruf bestätigen',cancel:'Abbrechen',
    warning:'Privy verlangt eine separate Wallet-Freigabe. Erst eine bestätigte Freigabe erlaubt spätere automatische Zahlungen; dieser Klick sendet keine Gebühr.',
    remote:'Nach lokalem Widerruf muss die Privy-Signer-Freigabe zusätzlich beim Wallet-Anbieter entfernt werden. Bereits gesendete Blockchain-Transaktionen bleiben bestehen.',
    ready:'Gebühren-Signer ist vom Server bestätigt. Automatische Zahlungen benötigen ausserdem die Live-Aktivierung und einen verifizierten Nettogewinn.',
    idle:'Nicht freigeschaltet',loading:'Berechtigung wird geprüft',failed:'Prüfung fehlgeschlagen',
    signed:'Einwilligung signiert, Policy kann veröffentlicht werden',
    unattached:'Policy veröffentlicht, Privy-Wallet-Freigabe steht noch aus',
    uncertain:'Privy-Rückmeldung unklar: nicht erneut veröffentlichen',
    revoked:'Lokal widerrufen',working:'Wird geprüft …',
    error:'Berechtigung nicht bestätigt. Wallet- und Serverstatus prüfen.',
  },
  en:{
    title:'5% profit fee · automatic',open:'Fee permission',
    intro:'Only 5% of actual realized net profit above your high-water mark, after verified trading costs. Fee permission is separate from trading and can be revoked locally.',
    cap:'Maximum per fee transfer',hint:'You choose this Privy permission cap; it is not a trade or daily limit.',
    authorize:'Sign fee consent',publish:'Publish fee policy',
    grant:'Approve Privy fee signer',revoke:'Revoke fee permission',
    confirmRevoke:'Confirm revocation',cancel:'Cancel',
    warning:'Privy requires a separate explicit wallet approval. Only a confirmed grant can later enable automatic payments; this button sends no fee.',
    remote:'After local revocation, remove the signer permission at Privy too. Blockchain transactions already sent cannot be undone.',
    ready:'Fee signer verified by backend. Automatic collection still requires live deployment and verified net profit.',
    idle:'Not enabled',loading:'Checking permission',failed:'Verification failed',
    signed:'Fee consent signed; ready for policy publication',
    unattached:'Fee policy published; wallet approval still required',
    uncertain:'Uncertain Privy result: do not publish again',
    revoked:'Revoked locally',working:'Checking …',
    error:'Permission not confirmed. Verify wallet and backend status.',
  },
};

export default function EvmFeeAutoOwnerConsent({
  asset,stable,address,userId,getAccessToken,enabled,lang='de',
}) {
  const {signMessage}=useSignMessage();
  const {addSigners}=useSigners();
  const txt=COPY[lang==='de'?'de':'en'];
  const [expanded,setExpanded]=useState(false);
  const [maxFee,setMaxFee]=useState('');
  const [status,setStatus]=useState(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [confirmRevoke,setConfirmRevoke]=useState(false);
  const allowed=enabled&&EVM_FEE_COINS.includes(asset)&&
    /^0x[0-9a-fA-F]{40}$/.test(address||'')&&typeof getAccessToken==='function';
  const api=async(path,method='GET',body)=>{
    const token=await getAccessToken();
    if(!token)throw Error('Privy token not available');
    const response=await fetch(API_BASE+path,{
      method,cache:'no-store',
      headers:{Authorization:'Bearer '+token,Accept:'application/json',
        ...(body?{'Content-Type':'application/json'}:{})},
      ...(body?{body:JSON.stringify(body)}:{}),
    });
    if(!response.ok)throw Error('MiniNex fee permission response unavailable');
    return response.json();
  };
  const endpoint='/api/live/evm/fee-auto-consent/'+asset;
  const refresh=async()=>setStatus(await api(endpoint));
  useEffect(()=>{
    let active=true;
    setStatus(null);setMaxFee('');setError('');setExpanded(false);setConfirmRevoke(false);
    if(!allowed)return()=>{active=false;};
    (async()=>{
      try{
        const result=await api(endpoint);
        if(active)setStatus(result);
      }catch{
        if(active)setError(txt.failed);
      }
    })();
    return()=>{active=false;};
  },[allowed,asset,userId,address,stable]);
  const action=async(run)=>{
    if(busy||!allowed)return;
    setBusy(true);setError('');
    try{await run();}
    catch(e){setError(e?.message||txt.error);}
    finally{
      try{await refresh();}catch{setError(txt.failed);}
      setBusy(false);
    }
  };
  const signIntent=()=>action(async()=>{
    if(status?.fee_auto_enrollment_exists!==false)return;
    const cap=exactFeeCapAtomic(maxFee,asset,stable);
    if(!cap)throw Error('Select an exact positive fee transfer maximum');
    const expires=Math.floor(Date.now()/1000)+29*86400;
    const challenge=await api(endpoint+'/challenge','POST',{
      max_fee_atomic:cap,policy_expires_unix:expires,
    });
    if(!validFeeAutoChallenge(challenge,{asset,stable,address,cap}))
      throw Error('Fee scope changed before wallet signature');
    // Separate direct user EIP191 approval; no trade or fee is sent.
    const signed=await signMessage({message:challenge.message_to_sign},{
      address,
    });
    if(!/^0x[0-9a-fA-F]{130}$/.test(signed?.signature||''))
      throw Error('Original wallet signature missing');
    const result=await api(endpoint+'/confirm','POST',{
      signature:signed.signature,
    });
    if(result?.state!=='CONSENT_SIGNED'||
       result?.owner_fee_intent_verified!==true||
       result?.automatic_fee_transfer_enabled!==false)
      throw Error('Fee intent not verified');
  });
  const publish=()=>action(async()=>{
    if(status?.state!=='CONSENT_SIGNED')return;
    const result=await api(endpoint+'/publish-policy','POST');
    if(result?.state!=='REMOTE_UNATTACHED'||
       result?.privy_wallet_owner_add_signer_still_required!==true||
       result?.automatic_fee_transfer_enabled!==false)
      throw Error('Exact owner fee policy not confirmed');
  });
  const grant=()=>action(async()=>{
    if(status?.state!=='REMOTE_UNATTACHED'||!status.privy_policy_id)return;
    const offer=await api(endpoint+'/grant-review');
    if(!validFeeAutoGrantOffer(offer,{
      asset,stable,address,policyId:status.privy_policy_id,
      cap:status.owner_max_fee_atomic,
    }))throw Error('Owner fee grant terms changed');
    // The owner explicitly authorizes the restricted policy in Privy's UI.
    await addSigners({
      address,signers:[{
        signerId:offer.execution_signer_id,policyIds:[offer.policy_id],
      }],
    });
    const result=await api(endpoint+'/grant-confirm','POST');
    if(result?.state!=='GRANT_VERIFIED'||
       result?.owner_fee_signer_granted!==true||
       result?.fee_payment_sent!==false)
      throw Error('Privy fee signer grant needs review');
  });
  const revoke=()=>action(async()=>{
    const result=await api(endpoint+'/revoke','POST');
    if(result?.future_automatic_fee_transactions_blocked!==true)
      throw Error('Fee revocation not verified');
    setConfirmRevoke(false);
  });
  if(!allowed)return null;
  const state=status?.state;
  const enabledGrant=state==='GRANT_VERIFIED'&&status?.privy_grant_verified;
  const statusText=enabledGrant?txt.ready:(
    state==='CONSENT_SIGNED'?txt.signed:
    state==='REMOTE_UNATTACHED'?txt.unattached:
    state==='REMOTE_UNCERTAIN'?txt.uncertain:
    state==='REVOKED'?txt.revoked:txt.idle);
  const decimals=asset==='BNB'?18:6;
  return <section className="privy-hwm-box" aria-label={txt.title}
    style={{display:'grid',gap:8}}>
    <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      onClick={()=>setExpanded(v=>!v)} aria-expanded={expanded}>
      {txt.open} · {asset}
    </button>
    {expanded&&<>
      <strong>{txt.title}</strong>
      <p className="privy-wallet-description">{txt.intro}</p>
      <p className="privy-wallet-description" role="status">
        {status?statusText:txt.loading}
      </p>
      {status?.fee_auto_enrollment_exists===false&&<>
        <label className="privy-wallet-description" style={{display:'grid',gap:5}}>
          {txt.cap} ({stable})
          <input type="text" inputMode="decimal"
            value={maxFee} disabled={busy} placeholder={decimals===18?'1.0':'1.00'}
            onChange={event=>setMaxFee(event.target.value)}/>
        </label>
        <small className="privy-wallet-description">{txt.hint}</small>
        <button type="button" className="privy-wallet-action" disabled={busy||
          !exactFeeCapAtomic(maxFee,asset,stable)} onClick={signIntent}>
          {txt.authorize}
        </button>
      </>}
      {state==='CONSENT_SIGNED'&&
        <button type="button" className="privy-wallet-action"
          disabled={busy} onClick={publish}>{txt.publish}</button>}
      {state==='REMOTE_UNATTACHED'&&<>
        <p className="privy-wallet-description" role="alert">{txt.warning}</p>
        <button type="button" className="privy-wallet-action"
          disabled={busy} onClick={grant}>{txt.grant}</button>
      </>}
      {status?.fee_auto_enrollment_exists===true&&state!=='REVOKED'&&<>
        {confirmRevoke?
          <div className="mininex-coin-wallet-actions">
            <button type="button" className="privy-wallet-action"
              disabled={busy} onClick={revoke}>{txt.confirmRevoke}</button>
            <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
              disabled={busy} onClick={()=>setConfirmRevoke(false)}>{txt.cancel}</button>
          </div>:
          <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
            disabled={busy} onClick={()=>setConfirmRevoke(true)}>{txt.revoke}</button>}
        <small className="privy-wallet-description">{txt.remote}</small>
      </>}
      {busy&&<p className="privy-wallet-description" role="status">{txt.working}</p>}
      {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
    </>}
  </section>;
}
