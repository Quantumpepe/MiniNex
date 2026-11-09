import React,{useEffect,useState} from 'react';
import {RefreshCcw,ShieldCheck} from 'lucide-react';
import {API_BASE} from './data.js';

const ASSETS=['BTC','ETH','USDC','USDT'];
const EXPECTED_NETWORK={BTC:'bitcoin-mainnet',ETH:'ethereum-mainnet',
                        USDC:'ethereum-mainnet',USDT:'ethereum-mainnet'};
const UNITS=/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;

export function validateWalletBalanceResponse(result) {
  if(!result||result.mode!=='VERIFIED_READ_ONLY'||
     result.wallet_ownership_verified!==true||
     result.real_trading_enabled!==false||
     result.automatic_signing_enabled!==false||
     result.wallet_funds_used_for_paper!==false||
     result.pricing!=='NATIVE_UNITS_ONLY'||!result.balances||
     !result.wallets||typeof result.wallets!=='object')return false;
  return ASSETS.every(asset=>{
    const entry=result.balances[asset];
    return entry&&entry.network===EXPECTED_NETWORK[asset]&&(
      (entry.status==='wallet_missing'&&entry.balance===null)||
      (entry.status==='verified'&&typeof entry.balance==='string'&&
       entry.balance.length<=85&&UNITS.test(entry.balance))
    );
  });
}

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
        if(!response.ok)throw new Error('Wallet balance source unavailable');
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
  return <div className="privy-hwm-box" aria-label={t.privyBalancesTitle}>
    <div className="privy-hwm-title"><strong><ShieldCheck size={17} aria-hidden="true"/> {t.privyBalancesTitle}</strong></div>
    <p className="privy-wallet-description">{t.privyBalancesNote}</p>
    {loading&&<p className="privy-wallet-description" role="status">{t.privyBalancesLoading}</p>}
    {status==='failed'&&<p className="privy-wallet-description" role="alert">{t.privyBalancesFailed}</p>}
    {status==='ready'&&<div className="privy-hwm-ledger">
      {ASSETS.map(asset=><div className="privy-hwm-row" key={asset}>
        <span><strong>{asset}</strong> <small className="privy-balance-network">{asset==='BTC'?'Bitcoin':'Ethereum'}</small></span>
        <strong>{balances[asset].status==='verified'
          ? balances[asset].balance+' '+asset
          : t.privyBalancesMissing}</strong>
      </div>)}
    </div>}
    <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      onClick={()=>setRequest(n=>n+1)} disabled={loading}>
      <RefreshCcw size={16} aria-hidden="true"/> {t.privyBalancesRefresh}
    </button>
  </div>;
}
