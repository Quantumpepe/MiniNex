import React,{useEffect,useState} from 'react';
import {API_BASE} from './data.js';

export default function EthLiveWalletEquity({
  walletAddress,userId,getAccessToken,enabled,t,
}){
  const [report,setReport]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  useEffect(()=>{setReport(null);setBusy(false);setError('');},[
    walletAddress,userId,enabled]);
  async function refresh(){
    if(!enabled||!walletAddress||busy)return;
    setBusy(true);setError('');
    try{
      const token=await getAccessToken();
      if(!token)throw Error('Privy login required');
      const res=await fetch(API_BASE+'/api/live/eth/real-wallet-value',{
        cache:'no-store',headers:{Authorization:'Bearer '+token}});
      if(!res.ok)throw Error(t.liveEthWalletEquityUnavailable);
      const data=await res.json();
      if(data.source!=='OWNER_VERIFIED_LIVE_ETH_WALLET'||
         data.asset!=='ETH'||data.network!=='ethereum-mainnet'||
         data.real_wallet_balances_verified!==true||
         data.reference_prices_verified!==true||
         data.includes_paper!==false||data.includes_other_chains!==false||
         !Array.isArray(data.balances)||
         data.balances.map(a=>a.asset).join(',')!=='ETH,WETH,USDC,USDT'||
         typeof data.total_reference_value_usd!=='string'||
         !/^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$/.test(
           data.total_reference_value_usd))
         throw Error(t.liveEthWalletEquityUnavailable);
      setReport(data);
    }catch(e){setError(e?.message||t.liveEthWalletEquityUnavailable);}
    finally{setBusy(false);}
  }
  return <div className="privy-hwm-box" style={{display:'grid',gap:8}}>
    <strong>{t.liveEthWalletEquityTitle}</strong>
    <p className="privy-wallet-description">{t.liveEthWalletEquityNote}</p>
    <button className="privy-wallet-action privy-wallet-action--secondary"
      type="button" disabled={!enabled||busy||!walletAddress}
      onClick={refresh}>{busy?'…':t.liveEthWalletEquityRefresh}</button>
    {report&&<>
      <strong>{t.liveEthWalletEquityTotal}: {
        report.total_reference_value_usd} USD</strong>
      {report.balances.map(entry=><p key={entry.asset}
        className="privy-wallet-description">
        {entry.asset}: {entry.native_units} · {
          entry.reference_value_usd} USD
      </p>)}
    </>}
    {error&&<p className="privy-wallet-description" role="alert">
      {error}</p>}
  </div>;
}
