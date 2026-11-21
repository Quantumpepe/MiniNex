import React,{useEffect,useState} from 'react';
import {API_BASE} from './data.js';
import {validOriginalNativeOrderHistory,nativeReceivedAmount}
  from './nativeLiveOrderHistoryRules.js';

// Read-only original native order journal, per-coin and per-Privy user.
// No deposit, retry, send, manual hash entry or fake profit.
export default function NativeLiveOrderHistory({
  asset,enabled,userId,getAccessToken,t,
}){
  const [data,setData]=useState(null);
  useEffect(()=>{
    let active=true;
    const abort=new AbortController();
    setData(null);
    if(!enabled||!userId||!['BTC','SOL','XRP'].includes(asset)||
       typeof getAccessToken!=='function')
      return()=>abort.abort();
    const read=async()=>{
      try{
        const token=await getAccessToken();
        if(!active||!token)return;
        const resp=await fetch(API_BASE+
          '/api/live/native/order-status/'+asset+'?limit=20',{
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
          cache:'no-store',signal:abort.signal,
        });
        if(!resp.ok)throw Error('Native order journal unavailable');
        const result=await resp.json();
        if(!validOriginalNativeOrderHistory(result,asset))
          throw Error('Native order journal unverified');
        if(active)setData(result);
      }catch{if(active)setData(null);}
    };
    void read();
    const interval=window.setInterval(()=>{void read();},20000);
    return()=>{active=false;window.clearInterval(interval);abort.abort();};
  },[asset,enabled,userId,getAccessToken]);
  if(!enabled||!['BTC','SOL','XRP'].includes(asset)||!data||
     data.orders.length===0)return null;
  const state=o=>o.original_funds_refunded?(
    t?.liveNativeOrderRefund||'Funds returned to original wallet'):
    o.destination_wallet_credited?(
      t?.liveNativeOrderConfirmed||'Receiving wallet credited'):
    o.status==='SOURCE_FINALIZED'?(
      t?.liveNativeOrderCrosschain||'Source confirmed; awaiting receiving chain'):
    o.status==='SUBMITTED'?(
      t?.liveNativeOrderSubmitted||'Original source submitted'):
    o.status==='RECONCILIATION_REQUIRED'?(
      t?.liveNativeOrderUncertain||'Broadcast unresolved'):
    o.status==='PREPARED'?(t?.liveNativeOrderPrepared||'Order prepared'):
    o.status==='SENDING'?(t?.liveNativeOrderSending||'Submitting source'):
    o.status==='CANCELLED'?(t?.liveNativeOrderCancelled||'Order cancelled'):
    o.status;
  return <section className="mininex-live-orders" aria-label={asset+' original live orders'}>
    <strong>{t?.liveNativeOrderTitle||'Original blockchain trades'} · {asset}</strong>
    <div style={{display:'grid',gap:6,maxHeight:260,overflowY:'auto',scrollbarWidth:'none'}}>
      {data.orders.map(o=><div key={o.intent_key} className="privy-wallet-description"
          style={{display:'grid',gap:2}}>
        <span>{o.side==='BUY'?(t?.livePlanBuy||'Buy'):(t?.livePlanSell||'Sell')}
          {' · '}{state(o)}</span>
        <span>{o.original_input_amount} {o.original_input_asset}
          {o.destination_wallet_credited&&
            ' → '+nativeReceivedAmount(o)+' '+o.destination_asset}
          {o.original_funds_refunded&&
            ' · '+nativeReceivedAmount(o)+' '+o.destination_asset}
        </span>
        {o.source_tx_hash&&<code style={{overflowWrap:'anywhere'}}>
          {t?.liveNativeOrderSource||'Source'} · {o.source_chain}: {o.source_tx_hash}
        </code>}
        {o.expected_source_tx_hash&&<code style={{overflowWrap:'anywhere'}}>
          {t?.liveNativeOrderExpectedSource||'Signed transaction ID; network outcome unknown'}
          {' · '}{o.source_chain}: {o.expected_source_tx_hash}
        </code>}
        {o.destination_tx_hash&&<code style={{overflowWrap:'anywhere'}}>
          {o.original_funds_refunded?
            (t?.liveNativeOrderReturn||'Refund'):
            (t?.liveNativeOrderDestination||'Destination')}
            {' · '}{o.destination_asset}: {o.destination_tx_hash}
        </code>}
      </div>)}
    </div>
  </section>;
}
