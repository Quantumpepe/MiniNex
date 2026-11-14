import React, {useEffect, useState} from 'react';
import {API_BASE} from './data.js';
import CoinGrid from './CoinGrid.jsx';
import {coinBySymbol} from './coins.js';
import ExtraCoinStrategyReplay from './ExtraCoinStrategyReplay.jsx';
import ExtraPaperComparison from './ExtraPaperComparison.jsx';
import {EXTRA_REPLAY_COINS,defaultReplayOption} from './extraReplayRules.js';
import {validSharedStrategyProfile,applySharedStrategyToExtra}
  from './sharedStrategyProfile.js';

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

export default function TradingSetupPreview({t,getAccessToken,enabled,userId,renderBudget,renderSessionControls,renderCoinAddress,marketSnapshot,syncedAt,lang='de',paperAvailable=true}) {
  const [settings,setSettings]=useState(INITIAL);
  const [saved,setSaved]=useState(null);
  const [status,setStatus]=useState('loading');
  const [busy,setBusy]=useState(false);
  const [globalProfile,setGlobalProfile]=useState(null);
  const [globalDraft,setGlobalDraft]=useState({mode:'LONG',horizon:'15m'});
  const [globalStatus,setGlobalStatus]=useState('loading');
  const [globalBusy,setGlobalBusy]=useState(false);
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

  useEffect(()=>{
    if(!enabled||!getAccessToken||!userId){
      setGlobalProfile(null);setGlobalStatus('loading');
      setGlobalDraft({mode:'LONG',horizon:'15m'});
      return;
    }
    let active=true;
    setGlobalStatus('loading');setGlobalProfile(null);
    (async()=>{
      try{
        const token=await getAccessToken();
        if(!token)throw Error('Login required');
        const response=await fetch(API_BASE+'/api/strategy/profile',{
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
          cache:'no-store',
        });
        if(!response.ok)throw Error('Shared strategy unavailable');
        const data=await response.json();
        if(!validSharedStrategyProfile(data))throw Error('Invalid shared strategy');
        if(active){
          setGlobalProfile(data);
          setGlobalDraft({mode:data.strategy_mode,horizon:data.horizon});
          setReplayOptions(previous=>applySharedStrategyToExtra(previous,data));
          setGlobalStatus('ready');
        }
      }catch{if(active){setGlobalStatus('failed');setGlobalProfile(null);}}
    })();
    return()=>{active=false;};
  },[enabled,getAccessToken,userId]);

  const applyGlobal=async()=>{
    if(!enabled||globalBusy||globalStatus!=='ready'||!globalProfile)return;
    setGlobalBusy(true);
    try{
      const token=await getAccessToken();
      if(!token)throw Error('Login required');
      const response=await fetch(API_BASE+'/api/strategy/profile',{
        method:'PUT',cache:'no-store',
        headers:{Authorization:'Bearer '+token,Accept:'application/json',
                 'Content-Type':'application/json'},
        body:JSON.stringify({...globalDraft,expected_revision:globalProfile.revision}),
      });
      if(!response.ok)throw Error('Could not save shared strategy');
      const next=await response.json();
      if(!validSharedStrategyProfile(next)||!next.core_settings||
         !['BTC','ETH'].every(coin=>{
           const row=next.core_settings[coin];
           return row&&MODES.includes(row.mode)&&HORIZONS.includes(row.horizon)&&
             STABLES.includes(row.stable)&&Number.isSafeInteger(row.revision);
         }))throw Error('Incomplete saved core strategy');
      setGlobalProfile(next);
      setGlobalDraft({mode:next.strategy_mode,horizon:next.horizon});
      setReplayOptions(previous=>applySharedStrategyToExtra(previous,next));
      // The API commits BOTH legacy BTC/ETH preferences in that same DB
      // transaction, so update their revision and selected options together.
      applyResponse({mode:'PREFERENCES_ONLY',trading_enabled:false,
                     signing_authorized:false,settings:next.core_settings});
      setGlobalStatus('ready');
    }catch{setGlobalStatus('failed');}
    finally{setGlobalBusy(false);}
  };

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

  const changeReplay=(asset,key,value)=>{
    if(!EXTRA_REPLAY_COINS.includes(asset)||!['mode','horizon','budget','stable'].includes(key))return;
    setReplayOptions(old=>({...old,[asset]:{...old[asset],[key]:value}}));
  };
  const selectedCoin=coinBySymbol(opened);
  const selectedSettings=opened==='BTC'||opened==='ETH'?settings[opened]:null;

  return <div className="privy-trading-settings" aria-label={t.privySetupTitle}>
    <div className="privy-trading-settings-heading">{t.privySetupTitle}</div>
    {enabled&&<div className="privy-hwm-box mininex-shared-strategy">
      <strong>{lang==='de'?'Eine Strategie für alle 11 Coins':'One strategy for all 11 coins'}</strong>
      <p className="privy-wallet-description">
        {lang==='de'
          ?'Modus und Zeithorizont einmal einstellen. Jeder Coin behält sein eigenes Budget und seine bisherigen Trades.'
          :'Set mode and time horizon once. Every coin retains its own budget and trade history.'}
      </p>
      <div className="privy-asset-settings-fields">
        <label><span>{t.privySetupMode}</span>
          <select value={globalDraft.mode} disabled={globalStatus!=='ready'||globalBusy}
            onChange={e=>setGlobalDraft(old=>({...old,mode:e.target.value}))}>
            <option value="LONG">{t.privySetupLong}</option>
            <option value="SPOT_SHORT">{t.privySetupSpotShort}</option>
            <option value="AUTO">AUTO (Long + Spot)</option>
          </select>
        </label>
        <label><span>{t.privySetupHorizon}</span>
          <select value={globalDraft.horizon} disabled={globalStatus!=='ready'||globalBusy}
            onChange={e=>setGlobalDraft(old=>({...old,horizon:e.target.value}))}>
            {HORIZONS.map(h=><option key={h} value={h}>{h}</option>)}
          </select>
        </label>
      </div>
      <button type="button" className="privy-wallet-action"
        disabled={globalStatus!=='ready'||globalBusy}
        onClick={applyGlobal}>
        {lang==='de'?'Für alle 11 Coins übernehmen':'Apply to all 11 coins'}
      </button>
      {globalStatus==='failed'&&<p role="alert" className="privy-wallet-description">
        {t.tradeSettingsFailed}
      </p>}
    </div>}
    {paperAvailable&&<ExtraPaperComparison t={t} enabled={enabled} getAccessToken={getAccessToken}
      userId={userId}/>}
    <CoinGrid t={t} lang={lang} selected={opened} onSelect={toggleAsset}
      markets={marketSnapshot} syncedAt={syncedAt} kind="wallet"/>
    {selectedCoin&&<section className="privy-asset-settings mininex-selected-asset"
      aria-label={selectedCoin.symbol+' '+t.privyAssetSettings}>
      <div className="mininex-selected-header">
        <strong>{selectedCoin.glyph} {selectedCoin.name} · {selectedCoin.symbol}</strong>
        <span>{selectedCoin.inProgress?t.coinInProgress:!paperAvailable?t.wallet:selectedCoin.paperSettingsReady?t.coinExistingSettings:t.extraReplayTitle}</span>
      </div>
      {renderCoinAddress?.(opened)}
      {paperAvailable&&(selectedSettings ? <>
        <div className="privy-asset-settings-fields">
          <label>
            <span>{t.privySetupMode}</span>
            <select value={selectedSettings.mode} disabled={status!=='ready'||busy}
              onChange={event=>change(opened,'mode',event.target.value)}>
              <option value="LONG">{t.privySetupLong}</option>
              <option value="SPOT_SHORT">{t.privySetupSpotShort}</option>
            <option value="AUTO">AUTO (Long + Spot)</option>
            </select>
          </label>
          <label>
            <span>{t.privySetupStable}</span>
            <select value={selectedSettings.stable} disabled={status!=='ready'||busy}
              onChange={event=>change(opened,'stable',event.target.value)}>
              {STABLES.map(value=><option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <label>
            <span>{t.privySetupHorizon}</span>
            <select value={selectedSettings.horizon} disabled={status!=='ready'||busy}
              onChange={event=>change(opened,'horizon',event.target.value)}>
              <option value="5m">5m</option><option value="15m">15m</option>
              <option value="1h">1h</option><option value="24h">24h</option>
            </select>
          </label>
        </div>
        <button className="privy-wallet-action privy-wallet-action--secondary"
          type="button" disabled={status!=='ready'||busy||!saved?.[opened]||
            ['mode','stable','horizon'].every(key=>selectedSettings[key]===saved[opened][key])}
          onClick={()=>save(opened)}>{t.privyBudgetSave} {opened}</button>
        {renderBudget?.(opened)}
        {renderSessionControls?.(opened)}
      </> : selectedCoin.inProgress ? <p className="privy-wallet-description" role="status">
        {t.coinPendingTradeSettings}
      </p> : <ExtraCoinStrategyReplay asset={opened} enabled={enabled}
        t={t} getAccessToken={getAccessToken} options={replayOptions[opened]} onChange={changeReplay}/>)}
    </section>}
    {paperAvailable&&status==='failed'&&<div role="alert" className="privy-wallet-description">{t.tradeSettingsFailed}</div>}
    {paperAvailable&&status==='failed'&&<button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      onClick={reload} disabled={busy}>{t.privyBudgetReload}</button>}
  </div>;
}
