import React,{useEffect,useState} from 'react';
import {RefreshCcw,ShieldCheck} from 'lucide-react';
import {API_BASE} from './data.js';
import {ASSETS,validateWalletBalanceResponse} from './walletBalanceValidation.js';

export default function WalletBalances({t,getAccessToken,userId}) {
  const [request,setRequest]=useState(0);
  const [status,setStatus]=useState('loading');
  const [balances,setBalances]=useState(null);

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

  const loading=status==='loading';
  return <div className="privy-hwm-box privy-balances-card" aria-label={t.privyBalancesTitle}>
    <div className="privy-hwm-title"><strong><ShieldCheck size={17} aria-hidden="true"/> {t.privyBalancesTitle}</strong></div>
    <p className="privy-wallet-description">{t.privyBalancesCompactNote}</p>
    {loading&&<p className="privy-wallet-description" role="status">{t.privyBalancesLoading}</p>}
    {status==='failed'&&<p className="privy-wallet-description" role="alert">{t.privyBalancesFailed}</p>}
    {status==='privy-setup'&&<p className="privy-wallet-description" role="alert">{t.privyBalancesPrivySetup}</p>}
    {status==='privy-failed'&&<p className="privy-wallet-description" role="alert">{t.privyBalancesPrivyFailed}</p>}
    {status==='ready'&&<div className="privy-hwm-ledger privy-balances-grid">
      {ASSETS.map(asset=><div className="privy-hwm-row privy-balance-item" key={asset}>
        <span className="privy-balance-asset"><strong>{asset}</strong> <small className="privy-balance-network">{asset==='BTC'?'Bitcoin':'Ethereum'}</small></span>
        <strong className="privy-balance-value">{balances[asset].status==='verified'
          ? balances[asset].balance+' '+asset
          : balances[asset].status==='wallet_missing'
            ? t.privyBalancesMissing
            : t.privyBalancesUnavailable}</strong>
      </div>)}
    </div>}
    <button type="button" className="privy-wallet-action privy-wallet-action--secondary privy-balance-refresh"
      onClick={()=>setRequest(n=>n+1)} disabled={loading}>
      <RefreshCcw size={16} aria-hidden="true"/> {t.privyBalancesRefresh}
    </button>
  </div>;
}
