import React,{useEffect,useRef,useState} from 'react';
import {ArrowLeftRight} from 'lucide-react';
import {API_BASE,formatAssetQuantity,formatNumber} from './data.js';
import {COINS} from './coins.js';
import EthLiveSwap from './EthLiveSwap.jsx';
import EvmLiveSwap from './EvmLiveSwap.jsx';
import CrossChainOwnerSwap from './CrossChainOwnerSwap.jsx';
import {MANUAL_SWAP_ASSETS,manualSwapQuoteRequest,validManualSwapQuote,
  manualSwapQuoteExpiryMs,liveEvmStableSupported} from './manualSwapRules.js';

// Manual provider quote previews remain read-only for every chain. Only the
// already implemented ETH/USDC/USDT route mounts its separately user-signed
// Privy wallet executor after the user selects an exact amount. This parent
// neither signs nor broadcasts transactions and never grants AUTO trading.
export default function ManualSwapPreview({t,lang='de',getAccessToken,enabled,userId,initialCoin='ETH',lockedCoin=false}) {
  const [open,setOpen]=useState(false);
  const [from,setFrom]=useState(initialCoin);
  const [to,setTo]=useState('USDC');
  const [amount,setAmount]=useState('');
  const [busy,setBusy]=useState(false);
  const [quote,setQuote]=useState(null);
  const [expired,setExpired]=useState(false);
  const [error,setError]=useState('');
  const requestId=useRef(0);
  const inFlight=useRef(null);
  const abortInFlight=()=>{
    const current=inFlight.current;
    inFlight.current=null;
    current?.abort();
  };
  const request=manualSwapQuoteRequest(from,to,amount);
  const manualEvmCoin=['BNB','POL','AVAX','ARB','LINK','QNT','UNI'];
  const stableCoins=['USDC','USDT'];
  const ownerEvmAsset=manualEvmCoin.includes(from)&&stableCoins.includes(to)?from:
    manualEvmCoin.includes(to)&&stableCoins.includes(from)?to:null;
  const ownerEvmSide=ownerEvmAsset
    ?(stableCoins.includes(from)?'BUY':'SELL'):null;
  const ownerEvmStable=ownerEvmAsset
    ?(stableCoins.includes(from)?from:to):null;
  const liveOwnerEvmPair=!!ownerEvmAsset&&
    liveEvmStableSupported(ownerEvmAsset,ownerEvmStable);
  const supportedPair=!!ownerEvmAsset||(
    MANUAL_SWAP_ASSETS.includes(from)&&
    MANUAL_SWAP_ASSETS.includes(to)&&
    from!==to&&!!manualSwapQuoteRequest(from,to,'1'));
  // Both directions on Ethereum mainnet only. BTC and all other networks
  // must never inherit a signer from an unrelated coin/chain quote.
  const ethManualPair=(
    (from==='ETH' && ['USDC','USDT'].includes(to)) ||
    (to==='ETH' && ['USDC','USDT'].includes(from))
  );
  const ethManualReady=enabled && ethManualPair && request?.coin==='ETH';

  // One local timeout per quote, with cleanup; never poll or refetch.
  useEffect(()=>{
    if(!quote)return;
    const timeout=setTimeout(()=>{
      setQuote(current=>current===quote?null:current);
      setExpired(true);
    },Math.max(0,quote.expiresAtMs-Date.now()));
    return ()=>clearTimeout(timeout);
  },[quote]);

  useEffect(()=>{
    requestId.current+=1;
    abortInFlight();
    setOpen(false);
    setQuote(null);
    setExpired(false);
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
    abortInFlight();
    const controller=new AbortController();
    inFlight.current=controller;
    // Only active during an explicit user-initiated quote request.
    const timeout=setTimeout(()=>controller.abort(),15_000);
    setBusy(true);
    setQuote(null);
    setExpired(false);
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
      if(!response.ok){
        if(response.status===409 && request.coin==='MULTICHAIN'){
          const info=await response.json().catch(()=>null);
          const minimum=info?.detail?.minimum_source_amount;
          if(info?.detail?.code==='PROVIDER_MINIMUM'&&
             info.detail.source_asset===request.from&&
             typeof minimum==='string'&&
             /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(minimum)){
            throw Error(lang==='de'
              ?'THORChain verlangt mindestens '+minimum+' '+request.from+
                ' für diesen Swap. Es wurde kein Swap gesendet.'
              :'THORChain requires at least '+minimum+' '+request.from+
                ' for this swap. No swap was sent.');
          }
        }
        if(response.status===503 && request.coin==='MULTICHAIN')
          throw Error(lang==='de'
            ?'Für dieses Coin-Paar ist derzeit kein Kurs beim Liquiditätsanbieter verfügbar. Es wurde kein Swap gesendet.'
            :'No live liquidity quote is available for this coin pair. No swap was sent.');
        throw Error('Provider quote unavailable');
      }
      const data=await response.json();
      if(!validManualSwapQuote(data,request))throw Error('Invalid quote response');
      const expiresAtMs=manualSwapQuoteExpiryMs(data,request);
      if(expiresAtMs===null)throw Error('Stale or unverified quote');
      if(id===requestId.current)setQuote({data,request,expiresAtMs});
    }catch(error) {
      if(id===requestId.current)setError(
        error?.message?.includes('No live liquidity quote')||
        error?.message?.includes('kein Kurs beim Liquiditätsanbieter')||
        error?.message?.includes('THORChain verlangt mindestens')||
        error?.message?.includes('THORChain requires at least')
          ?error.message:t.walletSwapFailed);
    }finally {
      clearTimeout(timeout);
      if(inFlight.current===controller)inFlight.current=null;
      if(id===requestId.current)setBusy(false);
    }
  };

  const items=[...COINS.map(coin=>({value:coin.symbol,
    label:coin.symbol+(coin.inProgress?' · '+t.coinInProgress:'')})),
    {value:'USDC',label:'USDC'},{value:'USDT',label:'USDT'}];
  // The selected coin is fixed; only its stablecoin pair is offered here.
  const visibleItems=lockedCoin
    ? items.filter(item=>[initialCoin,'USDC','USDT'].includes(item.value))
    : items;
  const estimated=quote?.data?.expected_output_amount||
    quote?.data?.estimated_output_amount;
  const minimum=quote?.data?.minimum_output_amount;
  const inputNumber=Number(quote?.request?.body?.amount);
  const outputNumber=Number(estimated);
  const unitRate=Number.isFinite(inputNumber)&&inputNumber>0&&
    Number.isFinite(outputNumber)&&outputNumber>0
    ?outputNumber/inputNumber:null;
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
      <form onSubmit={checkQuote} className="mininex-swap-form">
        <label><span>{t.walletSwapFrom}</span>
          <select value={from} onChange={event=>changeFrom(event.target.value)}>
            {visibleItems.map(item=><option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <button type="button" className="privy-wallet-action privy-wallet-action--secondary mininex-swap-flip"
          onClick={()=>{reset();setFrom(to);setTo(from);}}
          aria-label={t.walletSwapReverse}>
          <ArrowLeftRight size={16} aria-hidden="true"/>
        </button>
        <label><span>{t.walletSwapTo}</span>
          <select value={to} onChange={event=>changeTo(event.target.value)}>
            {visibleItems.map(item=><option key={item.value} value={item.value}>{item.label}</option>)}
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
        {!ethManualPair&&!liveOwnerEvmPair&&<button className="privy-wallet-action" type="submit"
          disabled={!enabled||!request||busy}>
          {busy?t.walletSwapChecking:t.walletSwapCheck}
        </button>}
      </form>
      {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
      {expired&&<p className="privy-wallet-description" role="status">{t.walletSwapExpired}</p>}
      {quoteCurrent&&<div className="mininex-swap-quote" role="status">
        <div className="mininex-swap-summary">
          <span>{t.walletSwapFrom}: <b>{formatAssetQuantity(quote.request.body.amount,quote.request.from,lang)} {quote.request.from}</b></span>
          <span>{t.walletSwapTo}: <b>{formatAssetQuantity(estimated,quote.request.to,lang)} {quote.request.to}</b></span>
        </div>
        {unitRate!=null&&<div>{t.walletSwapRate}: <b>
          1 {quote.request.from} ≈ {(quote.request.from==='POL'||quote.request.to==='POL')
            ?formatNumber(unitRate,lang,3)
            :new Intl.NumberFormat(lang==='de'?'de-CH':'en-US',{
              maximumFractionDigits:8}).format(unitRate)} {quote.request.to}
        </b></div>}
        {quote.request.coin==='ETH'
          ? <div>{t.walletSwapSlippage}: <b>{quote.data.slippage_tolerance_pct}%</b></div>
          : Number.isFinite(quote.data.quoted_price_impact_bps)
            ? <div>{t.walletSwapPriceImpact}: <b>{(quote.data.quoted_price_impact_bps/100).toFixed(2)}%</b></div>
            : null}
        {minimum&&<div>{t.walletSwapMinimum}: <b>{formatAssetQuantity(minimum,quote.request.to,lang)} {quote.request.to}</b></div>}
        {quote.request.coin==='MULTICHAIN'&&quote.data.mode!=='ETHEREUM_SETTLEMENT_CROSS_EVM_QUOTE'&&<>
          <button type="button" className="privy-wallet-action"
            disabled aria-disabled="true">{t.walletSwapAction}</button>
          <small className="privy-wallet-description">{t.walletSwapPendingShort}</small>
        </>}
      </div>}
      {ethManualReady&&<EthLiveSwap
        key={[userId,request.body.side,request.body.stable,request.body.amount].join(':')}
        enabled={enabled} userId={userId} getAccessToken={getAccessToken}
        side={request.body.side} stable={request.body.stable}
        amount={request.body.amount} t={t}/>}
      {liveOwnerEvmPair&&request&&<EvmLiveSwap
        key={[userId,ownerEvmAsset,ownerEvmSide,ownerEvmStable,amount].join(':')}
        enabled={enabled} userId={userId} asset={ownerEvmAsset}
        side={ownerEvmSide} stable={ownerEvmStable} amount={amount}
        lang={lang} getAccessToken={getAccessToken} t={t}/>}
      {ownerEvmAsset&&!liveOwnerEvmPair&&<CrossChainOwnerSwap
        key={[userId,ownerEvmAsset].join(':')} asset={ownerEvmAsset}
        quote={quoteCurrent?quote:null} enabled={enabled} userId={userId}
        getAccessToken={getAccessToken} lang={lang} onRequote={reset}/>} 
    </div>}
  </section>;
}
