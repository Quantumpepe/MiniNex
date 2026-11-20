import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {nativeReceiptRequest,validNativeReceiptResponse} from './nativeReceiptRules.js';

// Existing user-owned SOL/XRP sender-chain receipt only, not a trade-fill.
// This component never requests a signature, sends coins or polls in background.
export default function NativeReceiptCheck({asset,enabled,userId,getAccessToken,t}){
  const [open,setOpen]=useState(false);
  const [hash,setHash]=useState('');
  const [receipt,setReceipt]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState(false);
  const req=useRef({counter:0,controller:null});
  const reset=()=>{
    req.current.counter++;
    req.current.controller?.abort();
    req.current.controller=null;
    setReceipt(null);
    setError(false);
  };
  useEffect(()=>{
    reset();setOpen(false);setHash('');setBusy(false);
    return ()=>{req.current.counter++;req.current.controller?.abort();};
  },[asset,enabled,userId]);
  if(!['SOL','XRP'].includes(asset))return null;
  const request=nativeReceiptRequest(asset,hash.trim());
  const verify=async event=>{
    event.preventDefault();
    if(!enabled||busy||!request||!getAccessToken)return;
    const id=++req.current.counter;
    req.current.controller?.abort();
    const controller=new AbortController();
    req.current.controller=controller;
    const timeout=setTimeout(()=>controller.abort(),15000);
    setBusy(true);setError(false);setReceipt(null);
    try{
      const token=await getAccessToken();
      if(!token||controller.signal.aborted)throw Error('Sign-in required');
      const response=await fetch(API_BASE+request.path,{
        cache:'no-store',signal:controller.signal,
        headers:{Authorization:'Bearer '+token,Accept:'application/json'},
      });
      if(!response.ok)throw Error('Original chain receipt unavailable');
      const result=await response.json();
      if(!validNativeReceiptResponse(result,request))
        throw Error('Wrong or unverified original chain receipt');
      if(id===req.current.counter)setReceipt(result);
    }catch{
      if(id===req.current.counter)setError(true);
    }finally{
      clearTimeout(timeout);
      if(req.current.controller===controller)req.current.controller=null;
      if(id===req.current.counter)setBusy(false);
    }
  };
  return <div className="mininex-live-receipt-check">
    <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      aria-expanded={open} onClick={()=>{reset();setOpen(v=>!v);}}>
      {t.liveReceiptCheck} · {asset}
    </button>
    {open&&<>
      <p className="privy-wallet-description">{t.liveReceiptExplain}</p>
      <form className="mininex-extra-replay-form" onSubmit={verify}>
        <label><span>{t.liveReceiptHash}</span>
          <input type="text" inputMode="text" autoComplete="off"
            value={hash} maxLength={90} placeholder="Transaction ID"
            onChange={e=>{reset();setHash(e.target.value);}}/>
        </label>
        <button className="privy-wallet-action" type="submit"
          disabled={!enabled||busy||!request}>
          {busy?t.livePlanLoading:t.liveReceiptCheck}
        </button>
      </form>
      {error&&<p role="alert" className="privy-wallet-description">
        {t.liveReceiptUnavailable}
      </p>}
      {receipt&&<div role="status" className="mininex-swap-quote">
        <strong>{t.liveReceiptState}: {receipt.source_chain_finalized?'FINALIZED':'UNVERIFIED'}</strong>
        <p className="privy-wallet-description">
          {asset==='SOL'
            ? 'Slot: '+receipt.source_chain_slot
            : 'Ledger: '+receipt.source_chain_ledger}
        </p>
        <p className="privy-wallet-description">{t.liveReceiptNoTradeClaim}</p>
      </div>}
    </>}
  </div>;
}
