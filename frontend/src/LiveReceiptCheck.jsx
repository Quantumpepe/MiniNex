import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {receiptRequest,validReceiptResponse} from './liveReceiptRules.js';

// Read-only on-demand check for THIS logged-in account's BTC or ETH wallet.
// Does not authorize a trade and does not join two unrelated chain receipts.
export default function LiveReceiptCheck({asset,enabled,userId,getAccessToken,t,
  side='BUY',stable='USDC'}){
  const [open,setOpen]=useState(false);
  const [kind,setKind]=useState('ROUTER02');
  const [hash,setHash]=useState('');
  const [result,setResult]=useState(null);
  const [error,setError]=useState(false);
  const [busy,setBusy]=useState(false);
  const ref=useRef({id:0,abort:null});
  const reset=()=>{
    ref.current.id+=1;
    ref.current.abort?.abort();
    ref.current.abort=null;
    setResult(null);setError(false);
  };
  useEffect(()=>{
    reset();setHash('');setOpen(false);setKind('ROUTER02');setBusy(false);
    return()=>{ref.current.id+=1;ref.current.abort?.abort();};
  },[asset,enabled,userId]);
  const request=receiptRequest(asset,hash,kind,side,stable);
  const check=async event=>{
    event.preventDefault();
    if(!enabled||busy||!request||!getAccessToken)return;
    const id=++ref.current.id;
    ref.current.abort?.abort();
    const controller=new AbortController();
    ref.current.abort=controller;
    const timeout=setTimeout(()=>controller.abort(),15_000);
    setResult(null);setError(false);setBusy(true);
    try{
      const token=await getAccessToken();
      if(!token||controller.signal.aborted)throw Error('Login required');
      const response=await fetch(API_BASE+request.path,{
        method:'GET',cache:'no-store',signal:controller.signal,
        headers:{Authorization:'Bearer '+token,Accept:'application/json'},
      });
      if(!response.ok)throw Error('Receipt unavailable');
      const data=await response.json();
      if(!validReceiptResponse(data,request))throw Error('Invalid receipt');
      if(id===ref.current.id)setResult(data);
    }catch{
      if(id===ref.current.id)setError(true);
    }finally{
      clearTimeout(timeout);
      if(ref.current.abort===controller)ref.current.abort=null;
      if(id===ref.current.id)setBusy(false);
    }
  };
  if(!['BTC','ETH'].includes(asset))return null;
  const details=result?.mode==='BTC_MAINNET_WALLET_TX_OBSERVATION'
    ? (t.liveReceiptConfirmations+': '+result.confirmations_observed+
       ' · '+t.liveReceiptWalletNet+': '+result.net_wallet_movement_btc+' BTC')
    : result?.mode==='ETH_ROUTER02_ERC20_FILL_OBSERVATION'
      ? result.swap_erc20_leg_verified
        ? (t.liveReceiptReceived+': '+result.destination_received_amount+
           ' '+result.swap_output_asset)
        : t.liveReceiptNotFinal
      : result?.mode==='ETH_MAINNET_WETH9_CONVERSION_RECEIPT'
        ? result.confirmed_native_conversion
          ? (t.liveReceiptReceived+': '+result.weth9_event_amount+
             ' '+result.converted_asset)
          : t.liveReceiptNotFinal
        : '';
  return <div className="mininex-live-receipt-check">
    <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      aria-expanded={open} onClick={()=>{
        reset();setOpen(v=>!v);
      }}>{t.liveReceiptCheck}</button>
    {open&&<>
      <p className="privy-wallet-description">{t.liveReceiptExplain}</p>
      <form className="mininex-extra-replay-form" onSubmit={check}>
        {asset==='ETH'&&<label><span>{t.liveReceiptKind}</span>
          <select value={kind} onChange={event=>{
            reset();setKind(event.target.value);
          }}>
            <option value="ROUTER02">{t.liveReceiptSwap}</option>
            <option value="DEPOSIT">{t.liveReceiptWrap}</option>
            <option value="WITHDRAW">{t.liveReceiptUnwrap}</option>
          </select>
        </label>}
        <label><span>{t.liveReceiptHash}</span>
          <input type="text" inputMode="text" autoComplete="off"
            value={hash} maxLength={66} onChange={event=>{
              reset();setHash(event.target.value);
            }} placeholder={asset==='ETH'?'0x…':'Transaction ID'}/>
        </label>
        <button type="submit" className="privy-wallet-action"
          disabled={!enabled||busy||!request}>
          {busy?t.livePlanLoading:t.liveReceiptCheck}
        </button>
      </form>
      {error&&<p className="privy-wallet-description" role="alert">
        {t.liveReceiptUnavailable}</p>}
      {result&&<div role="status" className="mininex-swap-quote">
        <strong>{t.liveReceiptState}: {result.state||
          (result.explorer_reports_confirmed?'CONFIRMED':'UNCONFIRMED')}</strong>
        {details&&<p className="privy-wallet-description">{details}</p>}
        <p className="privy-wallet-description">{t.liveReceiptNoTradeClaim}</p>
      </div>}
    </>}
  </div>;
}
