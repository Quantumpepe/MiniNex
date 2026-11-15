import React,{useEffect,useState} from 'react';
import {RefreshCcw,ShieldCheck} from 'lucide-react';
import {API_BASE} from './data.js';
import {ASSETS,validateWalletBalanceResponse} from './walletBalanceValidation.js';
import {EVM_BALANCE_ASSETS,validateMulticoinBalanceResponse}
  from './multicoinBalanceValidation.js';
import {NATIVE_COINS,validNativeBalanceResponse} from './nativeBalanceValidation.js';

export default function WalletBalances({t,getAccessToken,userId,onSnapshot}) {
  const [request,setRequest]=useState(0);
  const [status,setStatus]=useState('loading');
  const [balances,setBalances]=useState(null);
  const [evmBalances,setEvmBalances]=useState(null);
  const [evmStatus,setEvmStatus]=useState('loading');
  const [nativeBalances,setNativeBalances]=useState(null);
  const [nativeStatus,setNativeStatus]=useState('loading');

  useEffect(()=>{
    if(!userId){setBalances(null);setStatus('idle');return;}
    let current=true;
    setBalances(null);
    setStatus('loading');
    (async()=>{
      try{
        const token=await getAccessToken();
        if(!token)throw new Error('No authenticated token');
        const response=await fetch(API_BASE+'/api/wallet/balances',{
          method:'GET',
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
          cache:'no-store'
        });
        if(!response.ok){
          // These explicit server errors identify configuration/Privy problems,
          // distinct from a verifiable on-chain zero balance.
          const problem=await response.json().catch(()=>null);
          if(!current)return;
          const detail=problem?.detail;
          if(response.status===503&&detail==='Private Privy wallet verification not configured'){
            setStatus('privy-setup');return;
          }
          if(response.status===503&&
             (detail==='Could not independently verify Privy wallet'||
              detail==='Verified wallet selection unavailable')){
            setStatus('privy-failed');return;
          }
          throw new Error('Wallet balance source unavailable');
        }
        const data=await response.json();
        if(!validateWalletBalanceResponse(data))
          throw new Error('Unverified wallet balances');
        if(current){setBalances(data.balances);setStatus('ready');}
      }catch{
        if(current){setBalances(null);setStatus('failed');}
      }
    })();
    return()=>{current=false;};
  },[getAccessToken,userId,request]);

  // A failure on any additional EVM network never hides BTC/ETH balances.
  useEffect(()=>{
    if(!userId){setEvmBalances(null);setEvmStatus('idle');return;}
    let current=true;
    setEvmBalances(null);setEvmStatus('loading');
    (async()=>{
      try{
        const token=await getAccessToken();
        if(!token)throw new Error('No authenticated token');
        const response=await fetch(API_BASE+'/api/wallet/multicoin-balances',{
          method:'GET',
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
          cache:'no-store',
        });
        if(!response.ok)throw new Error('Additional mainnet balances unavailable');
        const data=await response.json();
        if(!validateMulticoinBalanceResponse(data))
          throw new Error('Unverified additional network balances');
        if(current){setEvmBalances(data.assets);setEvmStatus('ready');}
      }catch{
        if(current){setEvmBalances(null);setEvmStatus('failed');}
      }
    })();
    return()=>{current=false;};
  },[getAccessToken,userId,request]);

  // Same wallet screen; network failures never affect BTC/ETH or the EVM panel.
  useEffect(()=>{
    if(!userId){setNativeBalances(null);setNativeStatus('idle');return;}
    let active=true;
    setNativeBalances(null);setNativeStatus('loading');
    (async()=>{
      try{
        const token=await getAccessToken();
        if(!token)throw new Error('No authenticated token');
        const response=await fetch(API_BASE+'/api/wallet/native-balances',{
          method:'GET',cache:'no-store',
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        });
        if(!response.ok)throw new Error('Native balances unavailable');
        const data=await response.json();
        if(!validNativeBalanceResponse(data))
          throw new Error('Invalid native mainnet balance response');
        if(active){setNativeBalances(data.assets);setNativeStatus('ready');}
      }catch{
        if(active){setNativeBalances(null);setNativeStatus('failed');}
      }
    })();
    return()=>{active=false;};
  },[getAccessToken,userId,request]);

  // Keep the coin holdings inside their selected coin window. All values
  // remain independently verified, read-only and isolated from PAPER.
  useEffect(()=>{
    if(!onSnapshot)return;
    onSnapshot({
      BTC:balances?.BTC||null,ETH:balances?.ETH||null,
      BNB:evmBalances?.BNB||null,POL:evmBalances?.POL||null,
      AVAX:evmBalances?.AVAX||null,LINK:evmBalances?.LINK||null,
      UNI:evmBalances?.UNI||null,
      SOL:nativeBalances?.SOL||null,XRP:nativeBalances?.XRP||null,
      TRX:nativeBalances?.TRX||null,DOGE:nativeBalances?.DOGE||null,
    });
  },[onSnapshot,balances,evmBalances,nativeBalances]);

  const loading=status==='loading'||evmStatus==='loading'||nativeStatus==='loading';
  return <details className="privy-hwm-box privy-balances-card mininex-real-balances"
    aria-label={t.privyBalancesTitle}>
    <summary className="mininex-balance-summary">
      <span><ShieldCheck size={17} aria-hidden="true"/> <strong>{t.privyBalancesTitle}</strong></span>

    </summary>
    <div className="mininex-balance-expanded">
      <p className="privy-wallet-description">{t.privyBalancesCompactNote}</p>
      {loading&&<p className="privy-wallet-description" role="status">{t.privyBalancesLoading}</p>}
      {status==='failed'&&<p className="privy-wallet-description" role="alert">{t.privyBalancesFailed}</p>}
      {status==='privy-setup'&&<p className="privy-wallet-description" role="alert">{t.privyBalancesPrivySetup}</p>}
      {status==='privy-failed'&&<p className="privy-wallet-description" role="alert">{t.privyBalancesPrivyFailed}</p>}
      {status==='ready'&&<div className="privy-hwm-ledger privy-balances-grid">
        {['USDC','USDT'].map(asset=><div className="privy-hwm-row privy-balance-item" key={asset}>
          <strong>{asset}</strong>
          <strong className="privy-balance-value">{balances[asset].status==='verified'
            ? balances[asset].balance+' '+asset
            : balances[asset].status==='wallet_missing'
              ? t.privyBalancesMissing:t.privyBalancesUnavailable}</strong>
        </div>)}
      </div>}
      <button type="button" className="privy-wallet-action privy-wallet-action--secondary privy-balance-refresh"
        onClick={()=>setRequest(n=>n+1)} disabled={loading}>
        <RefreshCcw size={16} aria-hidden="true"/> {t.privyBalancesRefresh}
      </button>
    </div>
  </details>;
}
