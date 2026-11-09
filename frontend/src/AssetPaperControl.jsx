import React,{useCallback,useEffect,useState} from 'react';
import {API_BASE} from './data.js';

// All commands target the authenticated user's existing PAPER session.
// No wallet signer, key, allowance, real exchange or LIVE route exists here.
const validSession=(data,asset)=>data&&
  (data.status==='NOT_STARTED'||data.asset===asset)&&
  data.paper_only===true&&data.real_trading_enabled===false&&
  data.wallet_funds_used===false&&
  ['NOT_STARTED','ACTIVE','PAUSED'].includes(data.status)&&
  (data.status==='NOT_STARTED'||['USDC','USDT'].includes(data.settlement));

function runtimeLabel(seconds){
  if(!Number.isSafeInteger(seconds)||seconds<0)return '—';
  const h=Math.floor(seconds/3600);
  const m=Math.floor(seconds%3600/60);
  const s=seconds%60;
  return [h,m,s].map((v,i)=>i===0?String(v):String(v).padStart(2,'0')).join(':');
}

export default function AssetPaperControl({asset,t,getAccessToken,enabled}){
  const [session,setSession]=useState(null);
  const [loadedAt,setLoadedAt]=useState(0);
  const [now,setNow]=useState(Date.now());
  const [ready,setReady]=useState(false);
  const [busy,setBusy]=useState(false);
  const [failed,setFailed]=useState(false);

  const refresh=useCallback(async()=>{
    if(!enabled)return;
    const token=await getAccessToken();
    if(!token)throw Error('Login required');
    const response=await fetch(API_BASE+'/api/user-paper/'+asset,{
      headers:{Authorization:'Bearer '+token,Accept:'application/json'},
      cache:'no-store'
    });
    if(!response.ok)throw Error('Session unavailable');
    const data=await response.json();
    if(!validSession(data,asset))throw Error('Invalid simulation response');
    return data;
  },[asset,enabled,getAccessToken]);

  useEffect(()=>{
    if(!enabled){setReady(false);setSession(null);setFailed(false);return;}
    let active=true;
    (async()=>{
      try{
        const response=await fetch(API_BASE+'/api/user-paper/availability',{cache:'no-store'});
        if(!response.ok)throw Error('Simulation availability unavailable');
        const availability=await response.json();
        if(availability?.paper_available!==true||
           availability?.live_trading_enabled!==false){
          if(active){setReady(false);setSession(null);}return;
        }
        const data=await refresh();
        if(active){setReady(true);setSession(data);setLoadedAt(Date.now());setFailed(false);}
      }catch{if(active){setReady(false);setSession(null);setFailed(true);}}
    })();
    const poll=window.setInterval(async()=>{
      if(!active||document.hidden)return;
      try{
        const data=await refresh();
        if(active&&data){setSession(data);setLoadedAt(Date.now());setFailed(false);}
      }catch{if(active)setFailed(true);}
    },20000);
    return()=>{active=false;window.clearInterval(poll);};
  },[enabled,refresh]);

  useEffect(()=>{
    if(!ready||session?.status!=='ACTIVE')return;
    const timer=window.setInterval(()=>setNow(Date.now()),1000);
    return()=>window.clearInterval(timer);
  },[ready,session?.status]);

  const command=async action=>{
    if(!ready||busy||!session)return;
    setBusy(true);setFailed(false);
    try{
      const token=await getAccessToken();
      if(!token)throw Error('Login required');
      const response=await fetch(API_BASE+'/api/user-paper/'+asset+'/'+action,{
        method:'POST',
        headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        cache:'no-store'
      });
      if(!response.ok)throw Error('Paper command failed');
      const data=await response.json();
      if(!validSession(data,asset))throw Error('Invalid paper state');
      setSession(data);setLoadedAt(Date.now());setNow(Date.now());
    }catch{setFailed(true);}
    finally{setBusy(false);}
  };

  if(!enabled||!ready&&!failed)return null;
  if(!ready)return <div className="privy-paper-action">
    <p role="alert" className="privy-wallet-description">{t.personalDemoFailed}</p>
  </div>;

  const seconds=session.runtime_tracked===true &&
    Number.isSafeInteger(session.runtime_active_seconds)
      ? session.runtime_active_seconds+
        (session.status==='ACTIVE'?Math.floor(Math.max(0,now-loadedAt)/1000):0)
      : null;
  const pending=session.pause_exit_pending===true;
  const stable=['USDC','USDT'].includes(session.settlement)?session.settlement:'USDC';

  return <div className="privy-paper-action" aria-label={asset+' '+t.personalDemoTitle}>
    <div className="privy-paper-action-top">
      <span>{t.privyPaperRuntime}</span><strong>{runtimeLabel(seconds)}</strong>
      <span className="privy-paper-action-state">
        {session.status==='ACTIVE'?t.privyPaperRunning:
         pending?t.privyPaperExitPending:
         session.status==='PAUSED'?t.privyPaperPaused:t.privyPaperNotStarted}
      </span>
    </div>
    {session.status==='NOT_STARTED'&&<button type="button"
      className="privy-wallet-action privy-wallet-action--secondary"
      disabled={busy} onClick={()=>command('start')}>{t.personalDemoStart} · {asset}</button>}
    {session.status==='ACTIVE'&&<button type="button"
      className="privy-wallet-action privy-paper-stop"
      disabled={busy} onClick={()=>command('pause')}>
      {t.privyPaperStopTo} {stable}
    </button>}
    {session.status==='PAUSED'&&!pending&&<button type="button"
      className="privy-wallet-action privy-wallet-action--secondary"
      disabled={busy} onClick={()=>command('resume')}>{t.personalDemoResume} · {asset}</button>}
    {pending&&<p className="privy-wallet-description" role="status">
      {t.privyPaperPendingInfo} {stable}.
    </p>}
    {session.status==='PAUSED'&&!pending&&session.pause_exit_completed===true&&
      <p className="privy-wallet-description">{t.privyPaperStableReady} {stable}.</p>}
    {session.status==='ACTIVE'&&
      <p className="privy-wallet-description">{t.privyPaperStopNote}</p>}
    {failed&&<p role="alert" className="privy-wallet-description">
      {t.personalDemoFailed}
      <button type="button" className="privy-paper-retry" onClick={async()=>{
        try{const data=await refresh();if(data){setSession(data);setLoadedAt(Date.now());setFailed(false);}}
        catch{setFailed(true);}
      }}>{t.privyBudgetReload}</button>
    </p>}
  </div>;
}
