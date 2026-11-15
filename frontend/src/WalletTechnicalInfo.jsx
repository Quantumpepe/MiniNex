import React,{useEffect,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE} from './data.js';

function displayUsd(value) {
  if(value===null||value===undefined||value==='')return '—';
  const amount=Number(value);
  return Number.isFinite(amount)&&amount>=0
    ? amount.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})
    : '—';
}

// The old wallet technical disclosure lives here on the existing Settings
// page. No new controls or trading authority are introduced.
export default function WalletTechnicalInfo({t}) {
  const {ready,authenticated,user,getAccessToken}=usePrivy();
  const [ledger,setLedger]=useState(null);
  useEffect(()=>{
    let active=true;
    setLedger(null);
    if(!ready||!authenticated||!user?.id)return()=>{active=false;};
    (async()=>{
      try{
        const token=await getAccessToken();
        if(!token)return;
        const res=await fetch(API_BASE+'/api/account',{
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
          cache:'no-store'
        });
        if(!res.ok)return;
        const data=await res.json();
        if(active&&data?.account_persistent===true&&
           data?.mode==='PAPER_ONLY_NO_LIVE_ORDERS')
          setLedger(data.ledger||null);
      }catch{/* Technical ledger remains unavailable instead of showing estimates. */}
    })();
    return()=>{active=false;};
  },[ready,authenticated,user?.id,getAccessToken]);
  return <details className="panel privy-disclosure privy-technical-section">
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
        {ledger&&<div className="privy-hwm-ledger">
          {['BTC','ETH'].map(asset=><div className="privy-hwm-row" key={asset}>
            <b>{asset}</b><span>{t.privyHwmValue}: {displayUsd(ledger[asset]?.high_water_mark_usd)} USD</span>
          </div>)}
        </div>}
      </div>
    </div>
  </details>;
}
