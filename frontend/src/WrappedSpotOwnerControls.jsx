import React,{useEffect,useRef,useState} from 'react';
import {useSigners,useSignMessage} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {startWrappedOwnerTrade,readWrappedOwnerSettings,validWrappedSettings,validWrappedLiveSession} from './wrappedSpotOwnerFlow.js';

// Real shared EVM session. Native send/swap wallets remain independent.
export default function WrappedSpotOwnerControls({asset,address,userId,enabled,getAccessToken,t,lang}){
  const {addSigners}=useSigners();
  const {signMessage}=useSignMessage();
  const [original,setOriginal]=useState(null);
  const [settings,setSettings]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState(false);
  const [session,setSession]=useState(null);
  const generation=useRef(0);
  const busyRef=useRef(false);
  const german=lang==='de';
  const request=async(path,method='GET',body)=>{
    const token=await getAccessToken();
    if(!token)throw Error('Login required');
    const response=await fetch(API_BASE+path,{method,cache:'no-store',
      headers:{Authorization:'Bearer '+token,Accept:'application/json',
        ...(body?{'Content-Type':'application/json'}:{})},
      ...(body?{body:JSON.stringify(body)}:{})});
    if(!response.ok)throw Error('Trading authorization unavailable');
    return response.json();
  };
  useEffect(()=>{
    const id=++generation.current;
    setOriginal(null);setSettings(null);setError(false);setSession(null);setBusy(false);
    if(!enabled||!userId||!address)return()=>{generation.current++;};
    const refresh=async()=>{
      if(busyRef.current)return;
      try{
        const state=await request('/api/live/evm/auto-session/'+asset);
        if(!validWrappedLiveSession(state,asset))throw Error('Unverified session');
        if(id===generation.current&&!busyRef.current)setSession(state);
      }catch{if(id===generation.current&&!busyRef.current)setError(true);}
    };
    readWrappedOwnerSettings(asset,request).then(row=>{
      if(id===generation.current){setOriginal(row);setSettings(row.settings);}
    }).catch(()=>{if(id===generation.current)setError(true);});
    void refresh();
    const timer=setInterval(()=>{if(!document.hidden)void refresh();},20000);
    return()=>{generation.current++;clearInterval(timer);};
  },[asset,address,userId,enabled,getAccessToken]);
  const change=(key,value)=>{
    setSettings(old=>({...old,[key]:value}));setError(false);
  };
  const start=async()=>{
    if(busyRef.current||!enabled||!original||!session||['ACTIVE','EXIT_PENDING'].includes(session.status)||!validWrappedSettings(asset,settings))return;
    const id=generation.current;
    busyRef.current=true;setBusy(true);setError(false);
    try{
      const result=await startWrappedOwnerTrade({asset,address,settings,original,
        request,addSigners,signMessage});
      if(id===generation.current){setOriginal(result.saved);setSettings(result.saved.settings);setSession(result.session);}
    }catch{
      if(id===generation.current){
        setError(true);
        try{
          const row=await readWrappedOwnerSettings(asset,request);
          if(id===generation.current)setOriginal(row);
        }catch{}
      }
    }finally{
      busyRef.current=false;
      if(id===generation.current)setBusy(false);
    }
  };
  const command=async action=>{
    if(busyRef.current||!session||!enabled)return;
    const id=generation.current;
    busyRef.current=true;setBusy(true);setError(false);
    try{
      await request('/api/live/evm/auto-session/'+asset+'/'+action,'POST');
      const current=await request('/api/live/evm/auto-session/'+asset);
      if(!validWrappedLiveSession(current,asset)||
        !(action==='pause'?current.status==='PAUSED':['EXIT_PENDING','STOPPED'].includes(current.status)))
        throw Error('Trading session change not confirmed');
      if(id===generation.current)setSession(current);
    }catch{if(id===generation.current)setError(true);}
    finally{busyRef.current=false;if(id===generation.current)setBusy(false);}
  };
  const locked=busy||['ACTIVE','EXIT_PENDING'].includes(session?.status);
  if(!enabled)return null;
  return <section className="mininex-extra-replay" aria-label={asset+' LIVE'}>
    <strong>{german?'LIVE-Handel':'LIVE trading'}</strong>
    {settings&&<>
      <div className="mininex-extra-replay-form">
        <label><span>{t.extraReplayBudget} (USD)</span>
          <input inputMode="decimal" type="text" value={settings.budget} disabled={locked}
            onChange={e=>change('budget',e.target.value)}/></label>
        <label><span>{t.privySetupMode}</span><select value={settings.mode}
          disabled={locked} onChange={e=>change('mode',e.target.value)}>
          <option value="LONG">{t.privySetupLong}</option>
          <option value="SPOT_SHORT">{t.privySetupSpotShort}</option>
          <option value="AUTO">{t.privySetupAuto}</option>
        </select></label>
        <label><span>{t.privySetupHorizon}</span><select value={settings.horizon}
          disabled={locked} onChange={e=>change('horizon',e.target.value)}>
          {['5m','15m','1h','24h'].map(x=><option key={x}>{x}</option>)}
        </select></label>
        <label><span>{t.privySetupStable||'Settlement'}</span><select value={settings.stable}
          disabled={locked} onChange={e=>change('stable',e.target.value)}>
          <option>USDC</option><option>USDT</option>
        </select></label>
      </div>
      <p className="privy-wallet-description">{german?
        'Handel über Ethereum mit gedeckten Token. Native Ein- und Auszahlungen bleiben separat.':
        'Trading on Ethereum uses backed tokens. Native deposits and withdrawals remain separate.'}</p>
      {session&&!['ACTIVE','EXIT_PENDING'].includes(session.status)&&
        <button type="button" className="privy-wallet-action" onClick={start}
          disabled={busy||!validWrappedSettings(asset,settings)}>
          {busy?(german?'Freigabe wird geprüft …':'Checking authorization …'):'Start Trade · '+asset}
        </button>}
      {session&&<p role="status" className="privy-wallet-description">
        {session.status==='ACTIVE'?(session.worker_attached?(german?'Handel aktiv':'Trading active'):
          (german?'Sitzung gestartet · Worker nicht bestätigt':'Session started · worker unconfirmed')):
          session.status==='EXIT_PENDING'?(german?'Ausstieg läuft':'Exit pending'):
          session.status==='PAUSED'?(german?'Pausiert':'Paused'):
          session.status==='STOPPED'?(german?'Beendet':'Stopped'):(german?'Nicht gestartet':'Not started')}
        {' · '}{session.original_blockchain_swaps_finalized} / {session.original_blockchain_swaps_sent}
      </p>}
      {session&&['ACTIVE','EXIT_PENDING'].includes(session.status)&&
        <button type="button" className="privy-wallet-action" disabled={busy}
          onClick={()=>command('pause')}>{t.extraPaperPause} · {asset}</button>}
      {session&&['ACTIVE','PAUSED'].includes(session.status)&&
        <button type="button" className="privy-wallet-action" disabled={busy}
          onClick={()=>command('exit')}>Stop & Exit · {asset}</button>}
    </>}
    {error&&<p role="alert" className="privy-wallet-description">{german?
      'Handelsfreigabe nicht bestätigt.':'Trading authorization not confirmed.'}</p>}
  </section>;
}
