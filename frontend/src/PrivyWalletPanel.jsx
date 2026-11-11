import React, {useEffect, useState} from 'react';
import {usePrivy, useWallets} from '@privy-io/react-auth';
import {useCreateWallet as useCreateExtendedWallet} from '@privy-io/react-auth/extended-chains';
import {Copy, LockKeyhole, ShieldCheck, Wallet} from 'lucide-react';
import {PRIVY_APP_ID} from './privyConfig.js';
import {API_BASE} from './data.js';
import TradingSetupPreview from './TradingSetupPreview.jsx';
import AssetPaperControl from './AssetPaperControl.jsx';
import WalletBalances from './WalletBalances.jsx';
import LiveEthControls from './LiveEthControls.jsx';
import {useAdditionalCoinWallets} from './useAdditionalCoinWallets.js';
import {evmNetworkAddress,validEvmAddress} from './nativeCoinAddresses.js';
import {linkedVerifiedNativeAddress} from './xrpAdaWalletAddresses.js';


// Copies the complete public address. It never invokes a wallet signer or
// enables deposits; a copy failure is explicitly shown to the user.
function CopyWalletAddress({address, network, t}) {
  const [copyStatus,setCopyStatus] = useState('idle');
  const [showFull,setShowFull] = useState(false);
  useEffect(()=>{setCopyStatus('idle');setShowFull(false);},[address]);
  if(!address) return null;
  const copyAddress = async () => {
    try {
      if (!navigator?.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(address);
      setCopyStatus('copied');
    } catch {
      setCopyStatus('failed');
    }
  };
  return <div className="privy-copy-address">
    <strong className="privy-wallet-network">{network}</strong>
    {showFull && <div className="privy-wallet-address"
      style={{overflowWrap:'anywhere',wordBreak:'break-all'}} aria-label={t.privyFullAddress}>{address}</div>}
    <div style={{display:'flex',flexWrap:'wrap',gap:8}}>
      <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
        onClick={()=>setShowFull(v=>!v)} aria-expanded={showFull}>
        {showFull ? t.privyHideAddress : t.privyShowAddress}
      </button>
      <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
        onClick={copyAddress} aria-label={t.privyCopyAddress}>
        <Copy size={18} aria-hidden="true"/>
      </button>
    </div>
    <div className="privy-wallet-description" role="status" aria-live="polite">
      {copyStatus==='copied' ? t.privyAddressCopied :
       copyStatus==='failed' ? t.privyCopyFailed : ''}
    </div>
  </div>;
}

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
    account?.type === 'wallet' && account?.chainType === 'bitcoin-segwit' &&
    isNativeBtcAddress(account.address)
  );
}

// This component is mounted only after Privy identity and account persistence
// are confirmed. No BTC signing, transactions or transfers are enabled.
function useNativeBtcWallet(user, enabled) {
  const {createWallet} = useCreateExtendedWallet();
  // Keep provisioning mounted while the wallet page is open even if no coin
  // details are expanded. Never reuse a newly-created address across accounts.
  const [createdWallet,setCreatedWallet] = useState(null);
  const [state,setState] = useState('checking');
  const [retry,setRetry] = useState(0);
  const attemptedFor = React.useRef(null);
  const existing = findNativeBtcWallet(user);
  const createdForUser = createdWallet?.userId===user?.id
    ? createdWallet.wallet : null;
  const address = existing?.address ||
    (isNativeBtcAddress(createdForUser?.address)?createdForUser.address:null);

  useEffect(()=>{
    const userId = user?.id;
    if (!enabled || !userId) return;
    if (address) {
      setState('created');
      return;
    }
    const attemptKey = userId + ':' + retry;
    if (attemptedFor.current === attemptKey) return;
    attemptedFor.current = attemptKey;
    let active = true;
    setState('creating');
    (async()=>{
      try {
        // Creates a user-owned native BTC address. No trade signing granted.
        const result = await createWallet({chainType: 'bitcoin-segwit'});
        if (!active) return;
        const wallet = result?.wallet;
        if (!wallet || wallet.chainType !== 'bitcoin-segwit' ||
            !isNativeBtcAddress(wallet.address)) {
          setState('failed');
          return;
        }
        setCreatedWallet({userId,wallet});
        setState('created');
      } catch {
        if (active) setState('failed');
      }
    })();
    return ()=>{active=false;};
  },[enabled,user?.id,address,createWallet,retry]);

  return {address,state,retry:()=>setRetry(n=>n+1)};
}

// Deliberately no sendTransaction, withdrawal or delegation path here.
// The existing backend is PAPER-only, even when a user signs in to Privy.
function ConnectedPrivyWallet({t,lang,marketSnapshot,syncedAt}) {
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
  const btcWallet = useNativeBtcWallet(
    user,ready&&authenticated&&identityStatus==='verified'&&databaseStatus==='stored');
  const extraWallets = useAdditionalCoinWallets(
    user,ready&&authenticated&&identityStatus==='verified'&&databaseStatus==='stored');
  const verifiedAccount = ready&&authenticated&&identityStatus==='verified'&&databaseStatus==='stored';
  const [verifiedXrpAda,setVerifiedXrpAda] = useState({userId:null,XRP:null,ADA:null});
  useEffect(()=>{
    let active=true;
    setVerifiedXrpAda({userId:null,XRP:null,ADA:null});
    if(!verifiedAccount||!user?.id)return()=>{active=false;};
    const expectedId=user.id;
    Promise.all([
      linkedVerifiedNativeAddress(user,'xrpl'),
      linkedVerifiedNativeAddress(user,'cardano'),
    ]).then(([XRP,ADA])=>{
      if(active)setVerifiedXrpAda({userId:expectedId,XRP,ADA});
    }).catch(()=>{
      if(active)setVerifiedXrpAda({userId:expectedId,XRP:null,ADA:null});
    });
    return()=>{active=false;};
  },[verifiedAccount,user?.id,user?.linkedAccounts]);

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

  // Each budget control is rendered next to the SAME coin's saved trading
  // preferences. There is still one authenticated, revision-checked budget
  // request per asset; moving the controls does not change authorization.
  const renderBudget = asset => <div className="privy-asset-budget"
    aria-label={asset+' '+t.privyBudgetTitle}>
    <label className="privy-asset-budget-label">
      <span className="privy-asset-budget-caption">
        <strong>{t.privyBudgetShortTitle}</strong>
        <strong>{previewBudgets[asset]} %</strong>
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
    {budgetStatus==='failed' && <div className="privy-wallet-description" role="alert">{t.privyBudgetFailed}</div>}
    {budgetStatus==='failed' && <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      disabled={budgetBusy} onClick={reloadBudgets}>{t.privyBudgetReload}</button>}
  </div>;

  // The address belongs to the coin's own open settings panel. No central
  // wallet-address section and no address from an unverified native chain.
  const renderCoinAddress = asset => {
    if(asset==='BTC')return <div className="mininex-coin-wallet-address">
      <div className="privy-wallet-description"><strong>{t.privyBtcTitle}</strong> · {
        btcWallet.address?t.privyBtcCreated:
        btcWallet.state==='creating'?t.privyBtcCreating:
        btcWallet.state==='failed'?t.privyBtcFailed:t.privyBtcPending
      }</div>
      {btcWallet.address &&
        <CopyWalletAddress address={btcWallet.address} network="BTC" t={t}/>}
      {btcWallet.state==='failed'&&!btcWallet.address&&
        <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
          onClick={btcWallet.retry}>{t.privyBtcRetry}</button>}
    </div>;
    if(asset==='ETH')return <div className="mininex-coin-wallet-address">
      <div className="privy-wallet-description">
        {address?t.privyConnected:(walletsReady?t.privyNoWallet:t.privyLoading)} · {t.privyEvm}
      </div>
      {address && <CopyWalletAddress address={address} network="ETH" t={t}/>}
    </div>;

    if(['BNB','POL','AVAX','LINK','UNI'].includes(asset)){
      const network=asset==='BNB'?'BNB Smart Chain':
        asset==='POL'?'Polygon PoS':asset==='AVAX'?'Avalanche C-Chain':
        asset==='LINK'?'Chainlink (Ethereum ERC-20)':'Uniswap (Ethereum ERC-20)';
      const chain=asset==='BNB'?'bsc-mainnet':
        asset==='POL'?'polygon-pos-mainnet':'avalanche-c-mainnet';
      const evmAddress=(asset==='LINK'||asset==='UNI')
        ? (validEvmAddress(address)?address:null)
        : evmNetworkAddress(chain,address);
      return <div className="mininex-coin-wallet-address" role="group"
        aria-label={asset+' '+t.privyFullAddress}>
        <p className="privy-wallet-description">{t.coinEvmSharedAddress}</p>
        {evmAddress
          ? <CopyWalletAddress address={evmAddress} network={network} t={t}/>
          : <div role="status" className="privy-wallet-description">
              {walletsReady?t.privyNoWallet:t.privyLoading}
            </div>}
        <p className="privy-wallet-description">{t.coinAddressReadOnly}</p>
      </div>;
    }
    if(asset==='SOL'||asset==='TRX'){
      const nativeWallet=asset==='SOL'?extraWallets.solana:extraWallets.tron;
      const network=asset==='SOL'?'Solana':'TRON';
      return <div className="mininex-coin-wallet-address" role="group"
        aria-label={asset+' '+t.privyFullAddress}>
        <div className="privy-wallet-description" role="status">
          {nativeWallet.address?t.coinWalletReady:
            nativeWallet.state==='creating'?t.coinWalletCreating:
            nativeWallet.state==='failed'?t.coinWalletFailed:t.privyBtcPending}
        </div>
        {nativeWallet.address&&
          <CopyWalletAddress address={nativeWallet.address} network={network} t={t}/>}
        {nativeWallet.state==='failed'&&!nativeWallet.address&&
          <button className="privy-wallet-action privy-wallet-action--secondary" type="button"
            onClick={nativeWallet.retry}>{t.coinWalletRetry}</button>}
        <p className="privy-wallet-description">{t.coinAddressReadOnly}</p>
      </div>;
    }
    // Until Privy supplies confirmed native XRPL/Cardano custody support,
    // only show already-linked, checksum-validated, account-owned addresses.
    // Never synthesize either from an EVM/SOL/TRON key.
    if(asset==='XRP'||asset==='ADA'){
      const nativeAddress=verifiedAccount&&verifiedXrpAda.userId===user?.id
        ? verifiedXrpAda[asset] : null;
      return <div className="mininex-coin-wallet-address" role="group"
        aria-label={asset+' '+t.privyFullAddress}>
        {nativeAddress
          ? <CopyWalletAddress address={nativeAddress}
              network={asset==='XRP'?'XRP Ledger':'Cardano'} t={t}/>
          : <div role="status" className="privy-wallet-description">
              {t.coinAddressPending}
            </div>}
        <p className="privy-wallet-description">{t.coinAddressReadOnly}</p>
      </div>;
    }
    // DOGE and other unsupported native wallets require dedicated verified custody.
    // Do not reuse an EVM/Solana address or display a fake receiving address.
    return <div className="mininex-coin-wallet-address" role="status">
      <div className="privy-wallet-description">{t.coinAddressPending}</div>
    </div>;
  };

  const renderSessionControls = asset =>
    <AssetPaperControl asset={asset} t={t} getAccessToken={getAccessToken}
      enabled={identityStatus==='verified' && databaseStatus==='stored'}/>;

  return <section className="panel privy-panel">
    <div className="privy-panel-header"><h2>{t.privyTitle}</h2></div>
    <div className="privy-panel-body privy-wallet-layout">
      {!ready ? <div className="privy-wallet-description">{t.privyLoading}</div> :
        !authenticated ? <button className="privy-wallet-action" type="button" onClick={()=>act(login)}>{t.privyLogin}</button> : <>
          {identityStatus==='verified' && databaseStatus==='stored' &&
            <WalletBalances t={t} getAccessToken={getAccessToken} userId={user?.id}/>}

          {(identityStatus==='failed' || identityStatus==='setup' || databaseStatus==='failed' || databaseStatus==='setup') &&
            <div className="privy-wallet-description" role="alert">{t.privyConnectionIssue}</div>}
          {(identityStatus==='failed' || identityStatus==='setup' ||
              (identityStatus==='verified' && databaseStatus!=='stored' && databaseStatus!=='checking')) &&
            <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
              onClick={()=>setRetryNonce(n=>n+1)}>{t.privyRetryConnection}</button>}

          <TradingSetupPreview t={t} getAccessToken={getAccessToken}
            enabled={identityStatus==='verified' && databaseStatus==='stored'}
            renderBudget={renderBudget} renderSessionControls={renderSessionControls}
            renderCoinAddress={renderCoinAddress}
            marketSnapshot={marketSnapshot} syncedAt={syncedAt} lang={lang}/>

          <LiveEthControls t={t} user={user} address={address}
            getAccessToken={getAccessToken}
            enabled={identityStatus==='verified' && databaseStatus==='stored'}/>

          <details className="privy-disclosure privy-technical-section">
            <summary>
              <span>{t.privyTechnicalTitle}</span>
              <small>{t.privyTechnicalHint}</small>
            </summary>
            <div className="privy-disclosure-body">
              <div className="privy-hwm-box" aria-label={t.privyTradingTitle}>
                <div className="privy-hwm-title"><strong>{t.privyTradingTitle}</strong></div>
                <p className="privy-wallet-description">{t.privyTradingNote}</p>
              </div>
              <div className="privy-hwm-box" aria-label={t.privyHwmTitle}>
                <div className="privy-hwm-title"><strong>{t.privyHwmTitle} · 5 %</strong></div>
                {databaseStatus==='stored' && assetLedger && <div className="privy-hwm-ledger">
                  {['BTC','ETH'].map(asset=><div className="privy-hwm-row" key={asset}>
                    <b>{asset}</b><span>{t.privyHwmValue}: {formatHwmUsd(assetLedger[asset]?.high_water_mark_usd)} USD</span>
                  </div>)}
                </div>}
              </div>
            </div>
          </details>
          <button className="privy-wallet-action privy-wallet-action--secondary privy-wallet-logout"
            type="button" onClick={()=>act(logout)}>{t.privyLogout}</button>
        </>}
      {error&&<p role="alert" className="privy-wallet-note">{t.privyError}</p>}
    </div>
  </section>;
}
export default function PrivyWalletPanel({t,lang,marketSnapshot,syncedAt}) {
  if(!PRIVY_APP_ID) return <section className="panel privy-panel">
    <div className="privy-panel-header"><h2>{t.privyTitle}</h2><Wallet size={19}/></div>
    <div className="privy-panel-body">
      <strong>{t.privyUnavailable}</strong>

    </div>
  </section>;
  return <ConnectedPrivyWallet t={t} lang={lang} marketSnapshot={marketSnapshot} syncedAt={syncedAt}/>;
}
