import React, {useEffect, useState} from 'react';
import {API_BASE} from './data.js';
import CoinGrid from './CoinGrid.jsx';
import {coinBySymbol} from './coins.js';
import ExtraCoinStrategyReplay from './ExtraCoinStrategyReplay.jsx';
import LiveBtcControls from './LiveBtcControls.jsx';
import ExtraPaperComparison from './ExtraPaperComparison.jsx';
import {EXTRA_REPLAY_COINS,defaultReplayOption,validExtraReplayBudget} from './extraReplayRules.js';
import {validSavedExtraCoinPreferences,savedExtraOptions,coinPreferenceChanged}
  from './extraCoinPreferenceRules.js';

// Persisted preferences are display/configuration only: no signer authority.
const INITIAL = Object.freeze({
  BTC: {mode:'LONG', stable:'USDC', horizon:'15m'},
  ETH: {mode:'LONG', stable:'USDC', horizon:'15m'},
});
const MODES = ['LONG','SPOT_SHORT','AUTO'];
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

export default function TradingSetupPreview({t,getAccessToken,enabled,userId,renderBudget,renderSessionControls,renderCoinAddress,renderLiveControls,renderCoinActions,renderCoinBalance,marketSnapshot,syncedAt,lang='de',paperAvailable=true}) {
  const [settings,setSettings]=useState(INITIAL);
  const [saved,setSaved]=useState(null);
  const [status,setStatus]=useState('loading');
  const [busy,setBusy]=useState(false);
  const [savedExtra,setSavedExtra]=useState(null);
  const [extraSettingsStatus,setExtraSettingsStatus]=useState('loading');
  const [extraSaving,setExtraSaving]=useState(null);
  const [extraRefresh,setExtraRefresh]=useState(0);
  // Retain independent virtual backtest settings when switching among coins.
  // These are local hypothetical budgets, not live or stored trade permissions.
  const [replayOptions,setReplayOptions]=useState(()=>Object.fromEntries(
    EXTRA_REPLAY_COINS.map(coin=>[coin,defaultReplayOption()])));
  // Only the coin button toggles this panel. Editing/saving fields never
  // closes it, and the selector itself scrolls independently of the form.
  const [opened,setOpened]=useState(null);
  const toggleAsset = asset => setOpened(current=>current===asset?null:asset);

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

  // All ten additional coins persist independently, including ARB.
  useEffect(()=>{
    let active=true;
    setSavedExtra(null);
    setExtraSettingsStatus('loading');
    setReplayOptions(Object.fromEntries(
      EXTRA_REPLAY_COINS.map(coin=>[coin,defaultReplayOption()])));
    if(!enabled||!getAccessToken||!userId)return()=>{active=false;};
    (async()=>{
      try{
        const token=await getAccessToken();
        if(!token)throw Error('Authentication required');
        const response=await fetch(API_BASE+'/api/coin-preferences',{
          method:'GET',cache:'no-store',
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        });
        if(!response.ok)throw Error('Individual settings unavailable');
        const data=await response.json();
        if(!validSavedExtraCoinPreferences(data))throw Error('Invalid saved settings');
        if(active){
          setSavedExtra(data.settings);
          setReplayOptions(savedExtraOptions(data));
          setExtraSettingsStatus('ready');
        }
      }catch{
        // Until Render deploy, keep the original individual-coin forms usable.
        if(active){setSavedExtra(null);setExtraSettingsStatus('failed');}
      }
    })();
    return()=>{active=false;};
  },[enabled,getAccessToken,userId,extraRefresh]);

  const saveExtra=async (asset,selected)=>{
    // Save the edited snapshot, not the value from before the last keystroke.
    selected=selected||replayOptions[asset];
    const prior=savedExtra?.[asset];
    if(!enabled||!getAccessToken||extraSettingsStatus!=='ready'||
       extraSaving||!EXTRA_REPLAY_COINS.includes(asset)||!prior||
       !coinPreferenceChanged(selected,prior)||
       !validExtraReplayBudget(selected?.budget))return;
    setExtraSaving(asset);
    try{
      const token=await getAccessToken();
      if(!token)throw Error('Authentication required');
      const response=await fetch(API_BASE+'/api/coin-preferences/'+asset,{
        method:'PUT',cache:'no-store',
        headers:{Authorization:'Bearer '+token,Accept:'application/json',
                 'Content-Type':'application/json'},
        body:JSON.stringify({
          budget_usd:Number(selected.budget),mode:selected.mode,
          stable:selected.stable,horizon:selected.horizon,
          expected_revision:prior.revision,
        }),
      });
      if(!response.ok)throw Error('Save failed');
      const next=await response.json();
      if(!validSavedExtraCoinPreferences(next))throw Error('Invalid saved settings');
      setSavedExtra(next.settings);
      setReplayOptions(old=>{
        // Never overwrite newer keystrokes with an earlier server response.
        const latest=old[asset];
        if(latest&&['mode','stable','horizon','budget'].some(
          key=>latest[key]!==selected[key]))return old;
        return {...old,[asset]:savedExtraOptions(next)[asset]};
      });
      setExtraSettingsStatus('ready');
    }catch{
      setExtraSettingsStatus('failed');
    }finally{setExtraSaving(null);}
  };

  const change=(asset,field,value)=>{
    const accepted=field==='mode'?MODES:field==='stable'?STABLES:
      field==='horizon'?HORIZONS:[];
    if(status!=='ready' || busy || !['BTC','ETH'].includes(asset) || !accepted.includes(value))return;
    setSettings(old=>{
      const next={...old,[asset]:{...old[asset],[field]:value}};
      // Auto-save on change so the user doesn't need a separate save click.
      setTimeout(()=>save(asset),0);
      return next;
    });
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

  const changeReplay=(asset,key,value)=>{
    if(!EXTRA_REPLAY_COINS.includes(asset)||!['mode','horizon','budget','stable'].includes(key))return;
    setReplayOptions(old=>({
      ...old,[asset]:{...old[asset],[key]:value},
    }));
  };
  // Autosave the latest complete amount after input, not a stale value
  // every keystroke. Coin saves are serialized without arbitrary caps.
  useEffect(()=>{
    if(!enabled||!getAccessToken||extraSettingsStatus!=='ready'||
       extraSaving||!savedExtra)return;
    const asset=EXTRA_REPLAY_COINS.find(coin=>
      validExtraReplayBudget(replayOptions[coin]?.budget)&&
      coinPreferenceChanged(replayOptions[coin],savedExtra[coin]));
    if(!asset)return;
    const value=replayOptions[asset];
    const timer=setTimeout(()=>{void saveExtra(asset,value);},400);
    return()=>clearTimeout(timer);
  },[enabled,getAccessToken,extraSettingsStatus,extraSaving,
     savedExtra,replayOptions]);
  const selectedCoin=coinBySymbol(opened);
  const selectedSettings=opened==='BTC'||opened==='ETH'?settings[opened]:null;

  return <div className="privy-trading-settings" aria-label={t.privySetupTitle}>
    <div className="privy-trading-settings-heading">{t.privySetupTitle}</div>
    {paperAvailable&&<ExtraPaperComparison t={t} enabled={enabled} getAccessToken={getAccessToken}
      userId={userId}/>}
    <CoinGrid t={t} lang={lang} selected={opened} onSelect={toggleAsset}
      markets={marketSnapshot} syncedAt={syncedAt} kind="wallet"/>
    {selectedCoin&&<section className="privy-asset-settings mininex-selected-asset"
      aria-label={selectedCoin.symbol+' '+t.privyAssetSettings}>
      <div className="mininex-selected-header" aria-label={selectedCoin.inProgress?t.coinInProgress:selectedCoin.symbol}>
        <strong>{selectedCoin.glyph} {selectedCoin.name} · {selectedCoin.symbol}</strong>
        {renderCoinBalance?.(opened)}
      </div>
      {renderCoinAddress?.(opened)}
      {renderCoinActions?.(opened)}
      {selectedCoin.inProgress ? <p className="privy-wallet-description" role="status">
        {t.coinPendingTradeSettings}
      </p> : selectedCoin && !selectedCoin.inProgress ? <ExtraCoinStrategyReplay asset={opened} enabled={enabled} liveMode={!paperAvailable}
        t={t} getAccessToken={getAccessToken} options={replayOptions[opened]||defaultReplayOption()}
        onChange={changeReplay} onSavePreference={saveExtra}
        preferenceStatus={extraSettingsStatus}
        preferenceSaved={savedExtra?.[opened]||null}
        preferenceSaving={extraSaving===opened}
        onRetryPreference={()=>setExtraRefresh(n=>n+1)}/> : null}
      
    </section>}
    {paperAvailable&&status==='failed'&&<div role="alert" className="privy-wallet-description">{t.tradeSettingsFailed}</div>}
    {paperAvailable&&status==='failed'&&<button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      onClick={reload} disabled={busy}>{t.privyBudgetReload}</button>}
  </div>;
}
