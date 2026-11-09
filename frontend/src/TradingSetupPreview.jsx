import React, {useEffect, useState} from 'react';
import {API_BASE,formatMoney,validMarket} from './data.js';
import {Bitcoin} from 'lucide-react';

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

export default function TradingSetupPreview({t,getAccessToken,enabled,renderBudget,renderSessionControls,marketSnapshot,syncedAt,lang='de'}) {
  const [settings,setSettings]=useState(INITIAL);
  const [saved,setSaved]=useState(null);
  const [status,setStatus]=useState('loading');
  const [busy,setBusy]=useState(false);
  const [opened,setOpened]=useState({BTC:false,ETH:false});
  const toggleAsset = asset => setOpened(old=>({...old,[asset]:!old[asset]}));

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

  return <div className="privy-trading-settings" aria-label={t.privySetupTitle}>
    <div className="privy-trading-settings-heading">{t.privySetupTitle}</div>
    {['BTC','ETH'].map(asset=>{
      // Use the exact same independently checked market snapshot as Home;
      // when prices are stale, never display a previous quote as "live".
      const quote=marketSnapshot?.[asset];
      const fresh=validMarket(quote)&&syncedAt>0&&Date.now()-syncedAt<20000;
      const price=fresh?formatMoney(quote.price_usdt,lang):'—';
      const open=opened[asset];
      return <section key={asset} className={'privy-asset-settings'+(open?' is-open':'')}
        aria-label={asset+' '+t.privyAssetSettings}>
        <button type="button" className="privy-asset-toggle"
          onClick={()=>toggleAsset(asset)}
          aria-expanded={open}
          aria-controls={'mininex-wallet-settings-'+asset}>
          <span className={'compact-token compact-token--'+asset.toLowerCase()} aria-hidden="true">
            {asset==='BTC'?<Bitcoin size={21}/>:<span className="compact-eth">◆</span>}
          </span>
          <span className="privy-asset-toggle-name">
            <strong>{asset==='BTC'?'Bitcoin':'Ethereum'}</strong>
            <small>{asset} · {t.privyAssetSettings}</small>
          </span>
          <span className="privy-asset-toggle-price" aria-label={asset+' '+t.market}>
            <strong>{price}</strong>
            <small>{fresh?t.privyPriceLive:t.privyPriceUnavailable}</small>
          </span>
        </button>
        <div id={'mininex-wallet-settings-'+asset} className="privy-asset-settings-content" hidden={!open}>
          <div className="privy-asset-settings-fields">
            <label>
              <span>{t.privySetupMode}</span>
              <select value={settings[asset].mode} disabled={status!=='ready'||busy}
                onChange={event=>change(asset,'mode',event.target.value)}>
                <option value="LONG">{t.privySetupLong}</option>
                <option value="SPOT_SHORT">{t.privySetupSpotShort}</option>
              </select>
            </label>
            <label>
              <span>{t.privySetupStable}</span>
              <select value={settings[asset].stable} disabled={status!=='ready'||busy}
                onChange={event=>change(asset,'stable',event.target.value)}>
                {STABLES.map(value=><option key={value} value={value}>{value}</option>)}
              </select>
            </label>
            <label>
              <span>{t.privySetupHorizon}</span>
              <select value={settings[asset].horizon} disabled={status!=='ready'||busy}
                onChange={event=>change(asset,'horizon',event.target.value)}>
                <option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option>
                <option value="24h">24h</option>
              </select>
            </label>
          </div>
          <button className="privy-wallet-action privy-wallet-action--secondary" type="button"
            disabled={status!=='ready'||busy||!saved?.[asset]||
              (['mode','stable','horizon'].every(key=>settings[asset][key]===saved[asset][key]))}
            onClick={()=>save(asset)}>{t.privyBudgetSave} {asset}</button>
          {renderBudget?.(asset)}
          {renderSessionControls?.(asset)}
        </div>
      </section>;
    })}
    {status==='failed'&&<div role="alert" className="privy-wallet-description">{t.tradeSettingsFailed}</div>}
    {status==='failed'&&<button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      onClick={reload} disabled={busy}>{t.privyBudgetReload}</button>}
  </div>;
}
