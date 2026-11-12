import React,{useEffect,useRef,useState} from 'react';
import {ArrowLeftRight} from 'lucide-react';
import {API_BASE} from './data.js';
import {COINS} from './coins.js';
import {MANUAL_SWAP_ASSETS,manualSwapQuoteRequest,validManualSwapQuote,manualSwapQuoteExpiryMs,validEthSwapFeeReview} from './manualSwapRules.js';

// Standalone MANUAL wallet swap preview. It never signs, approves, delegates,
// broadcasts, auto-swaps or changes the independent BTC/ETH trading budgets.
export default function ManualSwapPreview({t,lang='de',getAccessToken,enabled,userId}) {
  const [open,setOpen]=useState(false);
  const [from,setFrom]=useState('ETH');
  const [to,setTo]=useState('USDC');
  const [amount,setAmount]=useState('');
  const [busy,setBusy]=useState(false);
  const [quote,setQuote]=useState(null);
  const [expired,setExpired]=useState(false);
  const [feeReview,setFeeReview]=useState(null);
  const [feeStatus,setFeeStatus]=useState('idle');
  const [error,setError]=useState('');
  const requestId=useRef(0);
  const inFlight=useRef(null);
  const abortInFlight=()=>{
    const current=inFlight.current;
    inFlight.current=null;
    current?.abort();
  };
  const request=manualSwapQuoteRequest(from,to,amount);
  const supportedPair=MANUAL_SWAP_ASSETS.includes(from)&&
    MANUAL_SWAP_ASSETS.includes(to)&&
    from!==to&&!!manualSwapQuoteRequest(from,to,'1');

  // One local timeout per quote, with cleanup; never poll or refetch.
  useEffect(()=>{
    if(!quote)return;
    const timeout=setTimeout(()=>{
      setQuote(current=>current===quote?null:current);
      setExpired(true);
    },Math.max(0,quote.expiresAtMs-Date.now()));
    return ()=>clearTimeout(timeout);
  },[quote]);

  // A provider fee review is a short-lived snapshot, not an executable quote.
  // Only one timeout while visible, no second network request.
  useEffect(()=>{
    if(!feeReview)return;
    const timeout=setTimeout(()=>{
      setFeeReview(current=>current===feeReview?null:current);
      setFeeStatus('expired');
    },Math.max(0,feeReview.expiresAtMs-Date.now()));
    return ()=>clearTimeout(timeout);
  },[feeReview]);

  useEffect(()=>{
    requestId.current+=1;
    abortInFlight();
    setOpen(false);
    setQuote(null);
    setExpired(false);
    setFeeReview(null);
    setFeeStatus('idle');
    setError('');
    setBusy(false);
  },[userId,enabled]);

  // Cancel in-flight requests on navigation/unmount, without any background
  // network requests or periodic work.
  useEffect(()=>()=>{requestId.current+=1;abortInFlight();},[]);

  const reset=()=>{
    requestId.current+=1;
    abortInFlight();
    setQuote(null);
    setExpired(false);
    setFeeReview(null);
    setFeeStatus('idle');
    setError('');
    setBusy(false);
  };
  const changeFrom=value=>{reset();setFrom(value);};
  const changeTo=value=>{reset();setTo(value);};
  const changeAmount=value=>{reset();setAmount(value);};

  const checkQuote=async(event)=>{
    event.preventDefault();
    if(!enabled||!request||busy)return;
    const id=++requestId.current;
    const controller=new AbortController();
    inFlight.current=controller;
    // Only active during an explicit user-initiated quote request.
    const timeout=setTimeout(()=>controller.abort(),15_000);
    setBusy(true);
    setQuote(null);
    setExpired(false);
    setFeeReview(null);
    setFeeStatus('idle');
    setError('');
    try {
      const token=await getAccessToken();
      if(id!==requestId.current)return;
      if(controller.signal.aborted)throw Error('Quote request expired');
      if(!token)throw Error('Authentication required');
      const response=await fetch(API_BASE+request.path,{
        method:'POST',cache:'no-store',
        signal:controller.signal,
        headers:{
          Authorization:'Bearer '+token,
          Accept:'application/json',
          'Content-Type':'application/json',
        },
        body:JSON.stringify(request.body),
      });
      if(!response.ok)throw Error('Provider quote unavailable');
      const data=await response.json();
      if(!validManualSwapQuote(data,request))throw Error('Invalid quote response');
      const expiresAtMs=manualSwapQuoteExpiryMs(data,request);
      if(expiresAtMs===null)throw Error('Stale or unverified quote');
      if(id===requestId.current)setQuote({data,request,expiresAtMs});
    }catch {
      if(id===requestId.current)setError(t.walletSwapFailed);
    }finally {
      clearTimeout(timeout);
      if(inFlight.current===controller)inFlight.current=null;
      if(id===requestId.current)setBusy(false);
    }
  };

  // Explicit user action only. Existing server endpoint builds a simulated
  // unsigned ETH transaction, but does not return calldata, authorize or send.
  const checkFee=async()=>{
    if(!enabled||busy||!request||request.coin!=='ETH')return;
    const id=++requestId.current;
    const controller=new AbortController();
    inFlight.current=controller;
    const timeout=setTimeout(()=>controller.abort(),15_000);
    setBusy(true);
    setFeeReview(null);
    setFeeStatus('checking');
    try{
      const token=await getAccessToken();
      if(id!==requestId.current)return;
      if(controller.signal.aborted||!token)throw Error('Fee check unavailable');
      const response=await fetch(API_BASE+'/api/live/eth/swap-plan',{
        method:'POST',cache:'no-store',signal:controller.signal,
        headers:{
          Authorization:'Bearer '+token,
          Accept:'application/json',
          'Content-Type':'application/json',
        },
        body:JSON.stringify(request.body),
      });
      if(!response.ok)throw Error('Fee plan unavailable');
      const data=await response.json();
      if(!validEthSwapFeeReview(data,request))throw Error('Unverified ETH fee review');
      if(id===requestId.current){
        setFeeReview({data,expiresAtMs:Date.now()+30_000});
        setFeeStatus('ready');
      }
    }catch{
      if(id===requestId.current)setFeeStatus('failed');
    }finally{
      clearTimeout(timeout);
      if(inFlight.current===controller)inFlight.current=null;
      if(id===requestId.current)setBusy(false);
    }
  };

  const items=[...COINS.map(coin=>({value:coin.symbol,
    label:coin.symbol+(coin.inProgress?' · '+t.coinInProgress:'')})),
    {value:'USDC',label:'USDC'},{value:'USDT',label:'USDT'}];
  const estimated=quote?.data?.expected_output_amount||
    quote?.data?.estimated_output_amount;
  const minimum=quote?.data?.minimum_output_amount;
  const expiry=quote?.expiresAtMs;
  const quoteCurrent=quote&&Date.now()<quote.expiresAtMs&&
    (quote.request===request || (request&&quote.request.from===request.from&&
      quote.request.to===request.to&&quote.request.body.amount===request.body.amount));

  return <section className="mininex-wallet-swap">
    <button type="button" className="privy-wallet-action mininex-swap-open"
      aria-expanded={open} aria-controls="mininex-manual-swap"
      onClick={()=>{reset();setOpen(value=>!value);}}>
      <ArrowLeftRight size={18} aria-hidden="true"/>
      <span>{t.walletSwapTitle}</span>
    </button>
    {open&&<div id="mininex-manual-swap" className="mininex-swap-content">
      <p className="privy-wallet-description">{t.walletSwapManualOnly}</p>
      <form onSubmit={checkQuote} className="mininex-swap-form">
        <label><span>{t.walletSwapFrom}</span>
          <select value={from} onChange={event=>changeFrom(event.target.value)}>
            {items.map(item=><option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <button type="button" className="privy-wallet-action privy-wallet-action--secondary mininex-swap-flip"
          onClick={()=>{reset();setFrom(to);setTo(from);}}
          aria-label={t.walletSwapReverse}>
          <ArrowLeftRight size={16} aria-hidden="true"/>
        </button>
        <label><span>{t.walletSwapTo}</span>
          <select value={to} onChange={event=>changeTo(event.target.value)}>
            {items.map(item=><option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <label className="mininex-swap-amount"><span>{t.walletSwapAmount} · {from}</span>
          <input type="text" inputMode="decimal" autoComplete="off"
            value={amount} onChange={event=>changeAmount(event.target.value)}
            placeholder="0.00" maxLength={48}/>
        </label>
        {!supportedPair&&<p className="privy-wallet-description"
          role="status">{t.walletSwapUnsupported}</p>}
        {supportedPair&&amount&&!request&&<p className="privy-wallet-description"
          role="alert">{t.walletSwapInvalidAmount}</p>}
        <button className="privy-wallet-action" type="submit"
          disabled={!enabled||!request||busy}>
          {busy?t.walletSwapChecking:t.walletSwapCheck}
        </button>
      </form>
      {supportedPair&&request?.coin==='ETH'&&<button type="button"
        className="privy-wallet-action privy-wallet-action--secondary"
        disabled={!enabled||!request||busy} onClick={checkFee}>
        {feeStatus==='checking'?t.walletSwapFeeChecking:t.walletSwapFeeCheck}
      </button>}
      {feeStatus==='failed'&&<p className="privy-wallet-description" role="alert">
        {t.walletSwapFeeFailed}</p>}
      {feeStatus==='expired'&&<p className="privy-wallet-description" role="status">
        {t.walletSwapFeeExpired}</p>}
      {feeReview&&<div className="mininex-swap-quote" role="status">
        <strong>{t.walletSwapFeeTitle}</strong>
        {feeReview.data.state==='APPROVAL_OR_PERMIT_REQUIRED'
          ? <p className="privy-wallet-description">{t.walletSwapFeeNeedsApproval}</p>
          : <div>{t.walletSwapFeeMaximum}: <b>{feeReview.data.max_possible_gas_eth} ETH</b></div>}
        <p className="privy-wallet-description">{t.walletSwapFeeCaveat}</p>
      </div>}
      {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
      {expired&&<p className="privy-wallet-description" role="status">{t.walletSwapExpired}</p>}
      {quoteCurrent&&<div className="mininex-swap-quote" role="status">
        <strong>{t.walletSwapIndicative}</strong>
        <div className="privy-wallet-description">
          {quote.request.coin==='ETH'?t.walletSwapRouteEth:t.walletSwapRouteBtc}
        </div>
        {quote.request.coin==='ETH'
          ? <div>{t.walletSwapSlippage}: <b>{quote.data.slippage_tolerance_pct}%</b></div>
          : <div>{t.walletSwapPriceImpact}: <b>{(quote.data.quoted_price_impact_bps/100).toFixed(2)}%</b></div>}
        <div>{t.walletSwapEstimated}: <b>{estimated} {to}</b></div>
        {minimum&&<div>{t.walletSwapMinimum}: <b>{minimum} {to}</b></div>}
        {expiry&&<p className="privy-wallet-description">
          {t.walletSwapExpires}: {new Date(expiry).toLocaleTimeString(lang==='de'?'de-CH':'en-US')}
        </p>}
        <p className="privy-wallet-description">{t.walletSwapFeesUnknown}</p>
      </div>}
      <p className="privy-wallet-description mininex-swap-disabled-note" role="status">
        {t.walletSwapNotEnabled}
      </p>
    </div>}
  </section>;
}
