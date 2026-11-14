import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import EthPolicyRouteReadiness from './EthPolicyRouteReadiness.jsx';
import LiveReceiptCheck from './LiveReceiptCheck.jsx';
import {createLivePlanRequest,normalizedLiveStable,validLiveTradePlan,
  LIVE_SETTLEMENTS} from './liveTradePlanRules.js';

// Real provider prices shown UNDER the selected coin only.
// A price quote is NOT an order; Privy transaction signing is a separate step.
export default function LiveTradePlan({
  asset,defaultStable='USDC',enabled,getAccessToken,userId,t,
}) {
  const [side,setSide]=useState('BUY');
  const [stable,setStable]=useState(()=>normalizedLiveStable(asset,defaultStable));
  const [amount,setAmount]=useState('');
  const [quote,setQuote]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState(false);
  const [expired,setExpired]=useState(false);
  const ref=useRef({id:0,abort:null});
  const allowed=LIVE_SETTLEMENTS[asset]||[];
  const request=createLivePlanRequest(asset,side,stable,amount);
  const reset=()=>{
    ref.current.id+=1;
    ref.current.abort?.abort();
    ref.current.abort=null;
    setQuote(null);
    setError(false);
    setExpired(false);
  };
  useEffect(()=>{
    ref.current.id+=1;
    ref.current.abort?.abort();
    ref.current.abort=null;
    setSide('BUY');
    setStable(normalizedLiveStable(asset,defaultStable));
    setAmount('');
    setQuote(null);
    setBusy(false);
    setError(false);
    setExpired(false);
    return()=>{
      ref.current.id+=1;
      ref.current.abort?.abort();
      ref.current.abort=null;
    };
  },[asset,defaultStable,enabled,userId]);

  // An indicative market price is never indefinitely current.
  useEffect(()=>{
    if(!quote)return;
    const timer=setTimeout(()=>{
      setQuote(current=>current===quote?null:current);
      setExpired(true);
    },30_000);
    return()=>clearTimeout(timer);
  },[quote]);

  const changeSide=value=>{reset();setSide(value);};
  const changeStable=value=>{reset();setStable(value);};
  const changeAmount=value=>{reset();setAmount(value);};

  const check=async e=>{
    e.preventDefault();
    if(!enabled||busy||!request||!getAccessToken)return;
    const id=++ref.current.id;
    ref.current.abort?.abort();
    const abort=new AbortController();
    ref.current.abort=abort;
    const timeout=setTimeout(()=>abort.abort(),15_000);
    setBusy(true);setQuote(null);setError(false);setExpired(false);
    try{
      const token=await getAccessToken();
      if(!token||abort.signal.aborted)throw Error('Authentication required');
      const response=await fetch(API_BASE+request.path,{
        method:'POST',cache:'no-store',signal:abort.signal,
        headers:{
          Authorization:'Bearer '+token,
          Accept:'application/json','Content-Type':'application/json',
        },
        body:JSON.stringify(request.body),
      });
      if(!response.ok)throw Error('Quote unavailable');
      const result=await response.json();
      if(!validLiveTradePlan(result,request))throw Error('Invalid trade quote');
      if(id===ref.current.id)setQuote({request,result});
    }catch{
      if(id===ref.current.id)setError(true);
    }finally{
      clearTimeout(timeout);
      if(ref.current.abort===abort)ref.current.abort=null;
      if(id===ref.current.id)setBusy(false);
    }
  };

  if(!allowed.length)return null;
  const source=side==='BUY'?stable:asset;
  const output=side==='BUY'?asset:stable;
  const result=quote?.result;
  return <section className="mininex-live-trade-plan" aria-label={asset+' '+t.livePlanTitle}>
    <strong>{t.livePlanTitle}</strong>
    <p className="privy-wallet-description">{t.livePlanDescription}</p>
    <form onSubmit={check} className="mininex-extra-replay-form">
      <label><span>{t.livePlanSide}</span>
        <select value={side} disabled={!enabled||busy}
          onChange={e=>changeSide(e.target.value)}>
          <option value="BUY">{t.livePlanBuy}</option>
          <option value="SELL">{t.livePlanSell}</option>
        </select>
      </label>
      <label><span>{t.privySetupStable}</span>
        <select value={stable||''} disabled={!enabled||busy}
          onChange={e=>changeStable(e.target.value)}>
          {allowed.map(code=><option value={code} key={code}>{code}</option>)}
        </select>
      </label>
      <label><span>{t.livePlanAmount} · {source}</span>
        <input type="text" inputMode="decimal" autoComplete="off" maxLength={45}
          disabled={!enabled||busy} value={amount}
          onChange={e=>changeAmount(e.target.value)} placeholder="0.00"/>
      </label>
      <button className="privy-wallet-action" type="submit"
        disabled={!enabled||busy||!request}>
        {busy?t.livePlanLoading:t.livePlanCheck}
      </button>
    </form>
    {amount&&!request&&<p role="alert" className="privy-wallet-description">
      {t.livePlanInvalidAmount}</p>}
    {error&&<p role="alert" className="privy-wallet-description">
      {t.livePlanUnavailable}</p>}
    {expired&&<p role="status" className="privy-wallet-description">
      {t.livePlanExpired}</p>}
    {quote&&result&&<div className="mininex-swap-quote" role="status">
      <div className="mininex-swap-summary">
        <span>{t.walletSwapFrom}: <b>{quote.request.amount} {source}</b></span>
        <span>{t.walletSwapTo}: <b>{result.estimated_output_amount} {output}</b></span>
      </div>
      {result.minimum_output_amount&&
        <p className="privy-wallet-description">
          {t.walletSwapMinimum}: {result.minimum_output_amount} {output}</p>}
      <p className="privy-wallet-description">
        {result.wallet_funds_verified?t.livePlanWalletChecked:t.livePlanVenueOnly}
      </p>
      <p className="privy-wallet-description">{t.livePlanNotExecutable}</p>
      {asset==='ETH'&&<EthPolicyRouteReadiness
        request={quote.request} enabled={enabled} userId={userId}
        getAccessToken={getAccessToken} t={t}/>}
    </div>}
    {['BTC','ETH'].includes(asset)&&<LiveReceiptCheck
      asset={asset} enabled={enabled} userId={userId}
      getAccessToken={getAccessToken} t={t} side={side} stable={stable}/>}
  </section>;
}
