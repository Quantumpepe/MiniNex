import React,{useEffect,useRef,useState} from 'react';
import {useSigners,useSignMessage} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {authorizeWrappedOwnerTrade,readWrappedOwnerSettings,validWrappedSettings} from './wrappedSpotOwnerFlow.js';
import ExtraCoinPaperControl from './ExtraCoinPaperControl.jsx';

// Draft release: exact owner grant only. The real trading session will be
// enabled only when the live executor is connected; never claim ACTIVE here.
export default function WrappedSpotOwnerControls({asset,address,userId,enabled,getAccessToken,t,lang}){
  const {addSigners}=useSigners();
  const {signMessage}=useSignMessage();
  const [original,setOriginal]=useState(null);
  const [settings,setSettings]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState(false);
  const [granted,setGranted]=useState(false);
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
    setOriginal(null);setSettings(null);setError(false);setGranted(false);setBusy(false);
    if(!enabled||!userId||!address)return()=>{generation.current++;};
    readWrappedOwnerSettings(asset,request).then(row=>{
      if(id===generation.current){setOriginal(row);setSettings(row.settings);}
    }).catch(()=>{if(id===generation.current)setError(true);});
    return()=>{generation.current++;};
  },[asset,address,userId,enabled,getAccessToken]);
  const change=(key,value)=>{
    setSettings(old=>({...old,[key]:value}));setGranted(false);setError(false);
  };
  const start=async()=>{
    if(busyRef.current||!enabled||!original||!validWrappedSettings(asset,settings))return;
    const id=generation.current;
    busyRef.current=true;setBusy(true);setError(false);setGranted(false);
    try{
      const result=await authorizeWrappedOwnerTrade({asset,address,settings,original,
        request,addSigners,signMessage});
      if(id===generation.current){setOriginal(result.saved);setSettings(result.saved.settings);setGranted(true);}
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
  if(!enabled)return null;
  return <section className="mininex-extra-replay" aria-label={asset+' LIVE'}>
    <strong>{german?'LIVE-Handel':'LIVE trading'}</strong>
    {settings&&<>
      <div className="mininex-extra-replay-form">
        <label><span>{t.extraReplayBudget} (USD)</span>
          <input inputMode="decimal" type="text" value={settings.budget} disabled={busy}
            onChange={e=>change('budget',e.target.value)}/></label>
        <label><span>{t.privySetupMode}</span><select value={settings.mode}
          disabled={busy} onChange={e=>change('mode',e.target.value)}>
          <option value="LONG">{t.privySetupLong}</option>
          <option value="SPOT_SHORT">{t.privySetupSpotShort}</option>
          <option value="AUTO">{t.privySetupAuto}</option>
        </select></label>
        <label><span>{t.privySetupHorizon}</span><select value={settings.horizon}
          disabled={busy} onChange={e=>change('horizon',e.target.value)}>
          {['5m','15m','1h','24h'].map(x=><option key={x}>{x}</option>)}
        </select></label>
        <label><span>{t.privySetupStable||'Settlement'}</span><select value={settings.stable}
          disabled={busy} onChange={e=>change('stable',e.target.value)}>
          <option>USDC</option><option>USDT</option>
        </select></label>
      </div>
      <p className="privy-wallet-description">{german?
        'Handel über Ethereum mit gedeckten Token. Native Ein- und Auszahlungen bleiben separat.':
        'Trading on Ethereum uses backed tokens. Native deposits and withdrawals remain separate.'}</p>
      <button type="button" className="privy-wallet-action" onClick={start}
        disabled={busy||granted||!validWrappedSettings(asset,settings)}>
        {busy?(german?'Freigabe wird geprüft …':'Checking authorization …'):'Start Trade · '+asset}
      </button>
      {granted&&<p role="status" className="privy-wallet-description">{german?
        'Privy-Freigabe bestätigt. Handel noch nicht aktiv.':
        'Privy authorization verified. Trading is not active yet.'}</p>}
      <ExtraCoinPaperControl asset={asset} t={t} enabled={enabled} liveMode
        getAccessToken={getAccessToken} options={settings}/>
    </>}
    {error&&<p role="alert" className="privy-wallet-description">{german?
      'Handelsfreigabe nicht bestätigt.':'Trading authorization not confirmed.'}</p>}
  </section>;
}
