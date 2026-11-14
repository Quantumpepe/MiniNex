import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {validLiveBtcLimits,validatedLiveBtcDraft} from './liveBtcRules.js';

// BTC's own live cap editor, INSIDE the selected BTC coin settings only.
// Unlike ETH, there is no Bitcoin Privy policy or delegated signer UI yet.
// Saving caps does NOT activate BTC live trading or change the PAPER budget.
const FIELDS=['planned_capital_usd','max_order_usd','max_daily_turnover_usd'];
export default function LiveBtcControls({t,enabled,getAccessToken,userId}){
  const [limits,setLimits]=useState(null);
  const [draft,setDraft]=useState(null);
  const [phase,setPhase]=useState('loading');
  const [busy,setBusy]=useState(false);
  const [notice,setNotice]=useState('');
  const ref=useRef(0);
  const request=async(path,method='GET',body)=>{
    const token=await getAccessToken();
    if(!token)throw Error('Missing login');
    const response=await fetch(API_BASE+path,{
      method,cache:'no-store',
      headers:{Authorization:'Bearer '+token,Accept:'application/json',
        ...(body?{'Content-Type':'application/json'}:{})},
      ...(body?{body:JSON.stringify(body)}:{}),
    });
    if(!response.ok)throw Error('BTC LIVE limits unavailable');
    const data=await response.json();
    if(!validLiveBtcLimits(data))throw Error('Unverified native BTC limits');
    return data;
  };
  const apply=data=>{
    setLimits(data);
    setDraft(Object.fromEntries(FIELDS.map(key=>[key,data[key]])));
  };
  useEffect(()=>{
    const id=++ref.current;
    setLimits(null);setDraft(null);setPhase('loading');setNotice('');
    if(!enabled||!getAccessToken||!userId)
      return()=>{ref.current++;};
    (async()=>{
      try{
        const data=await request('/api/live/btc/limits');
        if(id===ref.current){apply(data);setPhase('ready');}
      }catch{
        if(id===ref.current)setPhase('failed');
      }
    })();
    return()=>{ref.current++;};
  },[enabled,getAccessToken,userId]);
  const save=async()=>{
    if(busy||phase!=='ready'||!limits)return;
    const validated=validatedLiveBtcDraft(draft);
    if(!validated)return;
    const id=ref.current;
    setBusy(true);setNotice('');
    try{
      const data=await request('/api/live/btc/limits','PUT',{
        ...validated,expected_revision:limits.revision,
      });
      if(id===ref.current){apply(data);setNotice(t.liveBtcSaved);}
    }catch{
      if(id===ref.current)setNotice(t.liveBtcSaveFailed);
    }finally{if(id===ref.current)setBusy(false);}
  };
  const stop=async()=>{
    if(busy||!enabled)return;
    const id=ref.current;
    setBusy(true);setNotice('');
    try{
      const data=await request('/api/live/btc/stop','POST');
      if(id===ref.current){apply(data);setNotice(t.liveBtcStopped);}
    }catch{
      if(id===ref.current)setNotice(t.liveBtcSaveFailed);
    }finally{if(id===ref.current)setBusy(false);}
  };
  const changed=FIELDS.some(field=>draft?.[field]!==limits?.[field]);
  const canSave=enabled&&phase==='ready'&&!busy&&changed&&!!validatedLiveBtcDraft(draft);
  return <section className="privy-hwm-box mininex-btc-live-limits" aria-label={t.liveBtcTitle}>
    <strong>{t.liveBtcTitle}</strong>
    <p className="privy-wallet-description">{t.liveBtcExplanation}</p>
    {phase==='loading'&&<p role="status" className="privy-wallet-description">{t.liveEthLoading}</p>}
    {phase==='failed'&&<p role="alert" className="privy-wallet-description">{t.liveBtcUnavailable}</p>}
    {phase==='ready'&&limits&&<>
      {FIELDS.map((field,index)=><label key={field} style={{display:'grid',gap:4,marginBottom:8}}>
        <span className="privy-wallet-description">{
          [t.liveEthCapital,t.liveEthPerOrder,t.liveEthDaily][index]} (USD)</span>
        <input type="text" inputMode="decimal" autoComplete="off"
          aria-label={[t.liveEthCapital,t.liveEthPerOrder,t.liveEthDaily][index]}
          value={draft?.[field]??''} disabled={busy}
          onChange={event=>setDraft(old=>({...old,[field]:event.target.value}))}/>
      </label>)}
      <p className="privy-wallet-description">{t.liveBtcPaused}</p>
      {changed&&!validatedLiveBtcDraft(draft)&&<p role="alert" className="privy-wallet-description">
        {t.liveEthInvalidLimits}</p>}
      <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
        onClick={save} disabled={!canSave}>{t.liveEthSaveLimits}</button>
      <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
        onClick={stop} disabled={busy||!enabled}>{t.liveBtcStop}</button>
      {notice&&<p role="status" className="privy-wallet-description">{notice}</p>}
    </>}
  </section>;
}
