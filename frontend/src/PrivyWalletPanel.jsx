import React, {useEffect, useState} from 'react';
import {usePrivy, useWallets} from '@privy-io/react-auth';
import {useCreateWallet as useCreateExtendedWallet} from '@privy-io/react-auth/extended-chains';
import {LockKeyhole, ShieldCheck, Wallet} from 'lucide-react';
import {PRIVY_APP_ID} from './privyConfig.js';
import {API_BASE} from './data.js';

function formatHwmUsd(value) {
  if (value === null || value === undefined || value === '') return '—';
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0
    ? amount.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})
    : '—';
}


const isNativeBtcAddress = (address) =>
  typeof address === 'string' &&
  /^(?:bc1q[ac-hj-np-z02-9]{38,59}|bc1p[ac-hj-np-z02-9]{58}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$/.test(address);

function findNativeBtcWallet(user) {
  return (user?.linkedAccounts || []).find(account =>
    account?.type === 'wallet' && account?.chainType === 'bitcoin' &&
    isNativeBtcAddress(account.address)
  );
}

// This component is mounted only after Privy identity and account persistence
// are confirmed. No BTC signing, transactions or transfers are enabled.
function NativeBtcWallet({user, t}) {
  const {createWallet} = useCreateExtendedWallet();
  const [createdWallet,setCreatedWallet] = useState(null);
  const [state,setState] = useState('checking');
  const [retry,setRetry] = useState(0);
  const attemptedFor = React.useRef(null);
  const existing = findNativeBtcWallet(user);
  const address = existing?.address || (isNativeBtcAddress(createdWallet?.address) ? createdWallet.address : null);

  useEffect(()=>{
    const userId = user?.id;
    if (!userId || address) {
      if (address) setState('created');
      return;
    }
    const attemptKey = userId + ':' + retry;
    if (attemptedFor.current === attemptKey) return;
    attemptedFor.current = attemptKey;
    let active = true;
    setState('creating');
    (async()=>{
      try {
        // No signer or server authorization is granted here.
        const result = await createWallet({chainType: 'bitcoin'});
        if (!active) return;
        const wallet = result?.wallet;
        if (!wallet || wallet.chainType !== 'bitcoin' || !isNativeBtcAddress(wallet.address)) {
          setState('failed');
          return;
        }
        setCreatedWallet(wallet);
        setState('created');
      } catch {
        if (active) setState('failed');
      }
    })();
    return ()=>{active=false};
  },[user?.id, address, createWallet, retry]);

  return <div className="privy-btc-wallet">
    <div className="privy-wallet-description"><strong>{t.privyBtcTitle}</strong> · {
      address ? t.privyBtcCreated :
      state==='creating' ? t.privyBtcCreating :
      state==='failed' ? t.privyBtcFailed : t.privyBtcPending
    }</div>
    {address && <div className="privy-wallet-address" title={t.privyBtcTitle}>{address.slice(0,12)}…{address.slice(-10)}</div>}
    {state==='failed' && !address && <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      onClick={()=>setRetry(n=>n+1)}>{t.privyBtcRetry}</button>}
    <div className="privy-wallet-note">{t.privyBtcSafety}</div>
  </div>;
}

// Deliberately no sendTransaction, withdrawal or delegation path here.
// The existing backend is PAPER-only, even when a user signs in to Privy.
function ConnectedPrivyWallet({t}) {
  const {ready, authenticated, user, login, logout, getAccessToken} = usePrivy();
  const {wallets, ready:walletsReady} = useWallets();
  const [error,setError]=useState(false);
  const [retryNonce,setRetryNonce]=useState(0);
  const [identityStatus,setIdentityStatus]=useState('idle');
  const [databaseStatus,setDatabaseStatus]=useState('idle');
  const [assetLedger,setAssetLedger]=useState(null);
  const [routeCapabilities,setRouteCapabilities]=useState(null);
  const [previewBudgets,setPreviewBudgets]=useState({BTC:0,ETH:0});
  const [savedBudgets,setSavedBudgets]=useState(null);
  const [budgetStatus,setBudgetStatus]=useState('idle');
  const [budgetBusy,setBudgetBusy]=useState(false);
  useEffect(()=>{
    if(!ready||!authenticated){setIdentityStatus('idle');setDatabaseStatus('idle');setAssetLedger(null);setRouteCapabilities(null);setSavedBudgets(null);setPreviewBudgets({BTC:0,ETH:0});setBudgetStatus('idle');return;}
    let active=true;
    setIdentityStatus('checking');
    setDatabaseStatus('idle');
    setAssetLedger(null);
    setRouteCapabilities(null);
    setBudgetStatus('loading');
    (async()=>{
      try {
        const token=await getAccessToken();
        if(!token) throw new Error('No access token');
        const response=await fetch(API_BASE+'/api/auth/me',{
          method:'GET',headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
        });
        if(!active) return;
        if(response.status===503){setIdentityStatus('setup');return;}
        if(!response.ok){setIdentityStatus('failed');return;}
        const status=await response.json();
        if(status?.authenticated!==true || status.mode!=='IDENTITY_ONLY'){
          if(active)setIdentityStatus('failed');
          return;
        }
        if(active){setIdentityStatus('verified');setDatabaseStatus('checking');}
        try{
          const accountResponse=await fetch(API_BASE+'/api/account',{
            method:'GET',headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
          });
          if(!active)return;
          if(accountResponse.status===503){setDatabaseStatus('setup');return;}
          if(!accountResponse.ok){setDatabaseStatus('failed');return;}
          const account=await accountResponse.json();
          if(!active)return;
          if(account?.account_persistent===true && account?.mode==='PAPER_ONLY_NO_LIVE_ORDERS'){
            setAssetLedger(account.ledger||null);
            setDatabaseStatus('stored');
            try{
              const prefsResponse=await fetch(API_BASE+'/api/budgets',{
                method:'GET',headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
              });
              if(!active)return;
              if(!prefsResponse.ok)throw new Error('Unavailable');
              const data=await prefsResponse.json();
              if(!(data?.mode==='PREFERENCES_ONLY' && data.trading_enabled===false &&
        data.signing_authorized===false && ['BTC','ETH'].every(asset=>
          Number.isInteger(data?.budgets?.[asset]?.percent) &&
          data.budgets[asset].percent>=0 && data.budgets[asset].percent<=100 &&
          Number.isInteger(data.budgets[asset].revision) &&
          data.budgets[asset].authorized===false)))throw new Error('Invalid budget settings');
              if(active){
                setSavedBudgets(data.budgets);
                setPreviewBudgets({BTC:data.budgets.BTC.percent,ETH:data.budgets.ETH.percent});
                setBudgetStatus('ready');
              }
            }catch{
              if(active){setSavedBudgets(null);setBudgetStatus('failed');}
            }
            // Read-only capability contract; never enable trading from wallet existence.
            try {
              const routesResponse=await fetch(API_BASE+'/api/trading/capabilities',{
                method:'GET',headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
              });
              if(!active)return;
              if(routesResponse.ok){
                const capabilities=await routesResponse.json();
                if(active && capabilities?.mode==='PREPARATION_ONLY' &&
                   capabilities.real_trading_enabled===false &&
                   capabilities.automated_signing_enabled===false &&
                   capabilities.routes?.BTC?.ready===false &&
                   capabilities.routes?.ETH?.ready===false){
                  setRouteCapabilities(capabilities);
                }
              }
            }catch{
              // Missing route status never implies trading is ready.
            }
          }else{
            setDatabaseStatus('failed');
          }
        }catch{
          if(active)setDatabaseStatus('failed');
        }
      } catch {
        if(active)setIdentityStatus('failed');
      }
    })();
    return()=>{active=false};
  },[ready,authenticated,getAccessToken,retryNonce]);
  const embedded = wallets.find(wallet=>wallet.walletClientType==='privy' && /^0x[0-9a-fA-F]{40}$/.test(wallet.address||''));
  const address = embedded?.address;
  const act = async (fn) => {try {setError(false); await fn();} catch {setError(true);}};
  const applyBudgetResponse = data => {
    if(!(data?.mode==='PREFERENCES_ONLY' && data.trading_enabled===false &&
        data.signing_authorized===false && ['BTC','ETH'].every(asset=>
          Number.isInteger(data?.budgets?.[asset]?.percent) &&
          data.budgets[asset].percent>=0 && data.budgets[asset].percent<=100 &&
          Number.isInteger(data.budgets[asset].revision) &&
          data.budgets[asset].authorized===false)))throw new Error('Invalid budget response');
    setSavedBudgets(data.budgets);
    setPreviewBudgets({BTC:data.budgets.BTC.percent,ETH:data.budgets.ETH.percent});
    setBudgetStatus('ready');
  };
  const saveBudget = async asset => {
    if(budgetBusy || budgetStatus!=='ready' || !savedBudgets?.[asset])return;
    setBudgetBusy(true);
    setBudgetStatus('saving');
    try{
      const token=await getAccessToken();
      if(!token)throw new Error('No token');
      const response=await fetch(API_BASE+'/api/budgets/'+asset,{
        method:'PUT',
        headers:{Authorization:'Bearer '+token,Accept:'application/json','Content-Type':'application/json'},
        cache:'no-store',
        body:JSON.stringify({percent:previewBudgets[asset],expected_revision:savedBudgets[asset].revision})
      });
      if(!response.ok)throw new Error('Unable to save');
      applyBudgetResponse(await response.json());
    }catch{setBudgetStatus('failed');}
    finally{setBudgetBusy(false);}
  };
  const reloadBudgets = async () => {
    setBudgetBusy(true);
    try{
      const token=await getAccessToken();
      if(!token)throw new Error('No token');
      const response=await fetch(API_BASE+'/api/budgets',{
        method:'GET',headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
      });
      if(!response.ok)throw new Error('Unavailable');
      applyBudgetResponse(await response.json());
    }catch{setBudgetStatus('failed');}
    finally{setBudgetBusy(false);}
  };

  return <section className="panel privy-panel">
    <div className="privy-panel-header"><h2>{t.privyTitle}</h2><ShieldCheck size={19} color="#54eabb"/></div>
    <div className="privy-panel-body">
      {!ready ? <div className="privy-wallet-description">{t.privyLoading}</div> :
        !authenticated ? <>
          <div className="privy-wallet-description">{t.privyNoDeposits}</div>
          <button className="privy-wallet-action" type="button" onClick={()=>act(login)}>{t.privyLogin}</button>
        </> : <>
          <div className="privy-wallet-description">{address?t.privyConnected:(walletsReady?t.privyNoWallet:t.privyLoading)} · {t.privyEvm}</div>
          {address&&<div className="privy-wallet-address" title={t.privyEvm}>{address.slice(0,12)}…{address.slice(-10)}</div>}
          {identityStatus==='verified' && databaseStatus==='stored'
            ? <NativeBtcWallet user={user} t={t}/>
            : <div className="privy-wallet-description">{t.privyBtcTitle} · {t.privyBtcPending}</div>}
          <div className={'privy-wallet-description '+(identityStatus==='verified'?'text-positive':'')} aria-live="polite">
            {identityStatus==='verified'?t.privyBackendVerified:
             identityStatus==='checking'?t.privyBackendChecking:
             identityStatus==='setup'?t.privyBackendSetup:t.privyBackendFailed}
          </div>
          <div className={'privy-wallet-description '+(databaseStatus==='stored'?'text-positive':'')} aria-live="polite">
            {databaseStatus==='stored'?t.privyDatabaseReady:
             databaseStatus==='checking'?t.privyDatabaseChecking:
             databaseStatus==='setup'?t.privyDatabaseSetup:t.privyDatabasePending}
          </div>
          {(identityStatus==='failed' || identityStatus==='setup' ||
              (identityStatus==='verified' && databaseStatus!=='stored' && databaseStatus!=='checking')) &&
            <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
              onClick={()=>setRetryNonce(n=>n+1)}>{t.privyRetryConnection}</button>}
          <div className="privy-wallet-note"><LockKeyhole size={13} style={{verticalAlign:'middle',marginRight:6}}/>{t.privyNoDeposits}</div>
          <button className="privy-wallet-action privy-wallet-action--secondary" type="button" onClick={()=>act(logout)}>{t.privyLogout}</button>
        </>}
      {authenticated && <div className="privy-hwm-box" aria-label={t.privyBudgetTitle}>
        <div className="privy-hwm-title"><strong>{t.privyBudgetTitle}</strong><span>{t.privyBudgetPreferences}</span></div>
        {['BTC','ETH'].map(asset=><div className="privy-hwm-row" key={asset}>
          <label style={{display:'flex',flexDirection:'column',gap:8,width:'100%'}}>
            <span style={{display:'flex',justifyContent:'space-between',gap:12}}>
              <strong>{asset}</strong><span>{previewBudgets[asset]} %</span>
            </span>
            <input type="range" min="0" max="100" step="5"
              value={previewBudgets[asset]}
              disabled={budgetStatus!=='ready' || budgetBusy}
              onChange={event=>setPreviewBudgets(old=>({...old,[asset]:Number(event.target.value)}))}
              aria-label={asset+' '+t.privyBudgetTitle}/>
          </label>
          <button className="privy-wallet-action privy-wallet-action--secondary" type="button"
            disabled={budgetStatus!=='ready' || budgetBusy || previewBudgets[asset]===savedBudgets?.[asset]?.percent}
            onClick={()=>saveBudget(asset)}>{t.privyBudgetSave} {asset}</button>
        </div>)}
        <div className="privy-wallet-description" aria-live="polite">{
          budgetStatus==='ready'?t.privyBudgetReady:
          budgetStatus==='saving'?t.privyBudgetSaving:
          budgetStatus==='loading'?t.privyBudgetLoading:t.privyBudgetFailed
        }</div>
        {budgetStatus==='failed' && <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
          disabled={budgetBusy} onClick={reloadBudgets}>{t.privyBudgetReload}</button>}
        <div className="privy-hwm-disclaimer">{t.privyBudgetNote}</div>
      </div>}
      {authenticated && <div className="privy-hwm-box" aria-label={t.privyTradingTitle}>
        <div className="privy-hwm-title"><strong>{t.privyTradingTitle}</strong><span>{t.privyTradingLocked}</span></div>
        {['BTC','ETH'].map(asset=><div className="privy-hwm-row" key={asset}>
          <b>{asset}</b><span>{routeCapabilities?.routes?.[asset]?.ready===false
            ? t.privyTradingPreparing : t.privyTradingUnverified}</span>
        </div>)}
        <div className="privy-hwm-disclaimer">{t.privyTradingNote}</div>
      </div>}
      <div className="privy-hwm-box" aria-label={t.privyHwmTitle}>
        <div className="privy-hwm-title"><strong>{t.privyHwmTitle}</strong><span>5 %</span></div>
        <div className="privy-wallet-description">{t.privyHwmDescription}</div>
        {authenticated && databaseStatus==='stored' && assetLedger && <div className="privy-hwm-ledger">
          {['BTC','ETH'].map(asset=><div className="privy-hwm-row" key={asset}>
            <b>{asset}</b><span>{t.privyHwmValue}: {formatHwmUsd(assetLedger[asset]?.high_water_mark_usd)} USD</span>
          </div>)}
        </div>}
        <div className="privy-hwm-disclaimer">{t.privyHwmPrepared}</div>
      </div>
      {error&&<p role="alert" className="privy-wallet-note">{t.privyError}</p>}
    </div>
  </section>;
}
export default function PrivyWalletPanel({t}) {
  if(!PRIVY_APP_ID) return <section className="panel privy-panel">
    <div className="privy-panel-header"><h2>{t.privyTitle}</h2><Wallet size={19}/></div>
    <div className="privy-panel-body">
      <strong>{t.privyUnavailable}</strong>
      <p className="privy-wallet-description">{t.privyUnavailableNote}</p>
      <p className="privy-wallet-note">{t.privyNoDeposits}</p>
    </div>
  </section>;
  return <ConnectedPrivyWallet t={t}/>;
}
