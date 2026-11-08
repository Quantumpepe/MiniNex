import React, {useEffect, useState} from 'react';
import {API_BASE} from './data.js';

// Persisted preferences are display/configuration only: no signer authority.
const INITIAL = Object.freeze({
  BTC: {mode:'LONG', stable:'USDC', horizon:'15m'},
  ETH: {mode:'LONG', stable:'USDC', horizon:'15m'},
});
const MODES = ['LONG','SPOT_SHORT'];
const STABLES = ['USDC','USDT'];
const HORIZONS = ['5m','15m','1h','24h'];

function validResponse(data) {
  return data?.mode==='PREFERENCES_ONLY' &&
    data?.trading_enabled===false && data?.signing_authorized===false &&
    ['BTC','ETH'].every(asset=> {
      const row=data?.settings?.[asset];
      return row && MODES.includes(row.mode) && STABLES.includes(row.stable) &&
        HORIZONS.includes(row.horizon) && Number.isInteger(row.revision) &&
        row.revision>=0 && row.authorized===false;
    });
}

export default function TradingSetupPreview({t,getAccessToken,enabled}) {
  const [settings,setSettings]=useState(INITIAL);
  const [saved,setSaved]=useState(null);
  const [status,setStatus]=useState('loading');
  const [busy,setBusy]=useState(false);

  const applyResponse=data=>{
    if(!validResponse(data))throw new Error('Invalid trade preferences');
    setSaved(data.settings);
    setSettings({
      BTC: {mode:data.settings.BTC.mode,stable:data.settings.BTC.stable,horizon:data.settings.BTC.horizon},
      ETH: {mode:data.settings.ETH.mode,stable:data.settings.ETH.stable,horizon:data.settings.ETH.horizon},
    });
    setStatus('ready');
  };

  useEffect(()=>{
    if(!enabled || !getAccessToken){
      setSaved(null);setSettings(INITIAL);setStatus('loading');
      return;
    }
    let active=true;
    setStatus('loading');
    (async()=>{
      try {
        const token=await getAccessToken();
        if(!token)throw new Error('Authentication required');
        const response=await fetch(API_BASE+'/api/trade-preferences',{
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
        });
        if(!response.ok)throw new Error('Settings unavailable');
        const data=await response.json();
        if(active)applyResponse(data);
      }catch{if(active){setSaved(null);setStatus('failed');}}
    })();
    return()=>{active=false;};
  },[enabled,getAccessToken]);

  const change=(asset,field,value)=>{
    const accepted=field==='mode'?MODES:field==='stable'?STABLES:
      field==='horizon'?HORIZONS:[];
    if(status!=='ready' || busy || !['BTC','ETH'].includes(asset) || !accepted.includes(value))return;
    setSettings(old=>({...old,[asset]:{...old[asset],[field]:value}}));
  };

  const save=async asset=>{
    if(status!=='ready' || busy || !saved?.[asset])return;
    setBusy(true);
    try{
      const token=await getAccessToken();
      if(!token)throw new Error('Authentication required');
      const response=await fetch(API_BASE+'/api/trade-preferences/'+asset,{
        method:'PUT',
        headers:{Authorization:'Bearer '+token,Accept:'application/json','Content-Type':'application/json'},
        cache:'no-store',
        body:JSON.stringify({...settings[asset],expected_revision:saved[asset].revision})
      });
      if(!response.ok)throw new Error('Save failed');
      applyResponse(await response.json());
    }catch{setStatus('failed');}
    finally{setBusy(false);}
  };

  const reload=async()=>{
    if(busy)return;
    setBusy(true);
    try{
      const token=await getAccessToken();
      if(!token)throw new Error('Authentication required');
      const response=await fetch(API_BASE+'/api/trade-preferences',{
        headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
      });
      if(!response.ok)throw new Error('Refresh failed');
      applyResponse(await response.json());
    }catch{setStatus('failed');}
    finally{setBusy(false);}
  };

  return <section className="privy-hwm-box" aria-label={t.privySetupTitle}>
    <div className="privy-hwm-title"><strong>{t.privySetupTitle}</strong></div>
    {['BTC','ETH'].map(asset=><div key={asset} className="privy-setup-asset"
      style={{display:'flex',flexDirection:'column',gap:10,marginBottom:18}}>
      <strong>{asset}</strong>
      <label style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:10}}>
        <span>{t.privySetupMode}</span>
        <select value={settings[asset].mode} disabled={status!=='ready'||busy}
          onChange={event=>change(asset,'mode',event.target.value)}>
          <option value="LONG">{t.privySetupLong}</option>
          <option value="SPOT_SHORT">{t.privySetupSpotShort}</option>
        </select>
      </label>
      <label style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:10}}>
        <span>{t.privySetupStable}</span>
        <select value={settings[asset].stable} disabled={status!=='ready'||busy}
          onChange={event=>change(asset,'stable',event.target.value)}>
          {STABLES.map(value=><option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <label style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:10}}>
        <span>{t.privySetupHorizon}</span>
        <select value={settings[asset].horizon} disabled={status!=='ready'||busy}
          onChange={event=>change(asset,'horizon',event.target.value)}>
          <option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option>
          <option value="24h">24h</option>
        </select>
      </label>
      <button className="privy-wallet-action privy-wallet-action--secondary" type="button"
        disabled={status!=='ready'||busy||!saved?.[asset]||
          (['mode','stable','horizon'].every(key=>settings[asset][key]===saved[asset][key]))}
        onClick={()=>save(asset)}>{t.privyBudgetSave} {asset}</button>
    </div>)}
    {status==='failed'&&<div role="alert" className="privy-wallet-description">{t.tradeSettingsFailed}</div>}
    {status==='failed'&&<button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      onClick={reload} disabled={busy}>{t.privyBudgetReload}</button>}
  </section>;
}
