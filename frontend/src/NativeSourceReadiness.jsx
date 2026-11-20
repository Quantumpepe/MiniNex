import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {nativeSourcePreflightRequest,validNativeSourcePreflight,
  originalNativeBlocker} from './nativeSourceReadinessRules.js';

// Optional owner-initiated source-chain check for the CURRENT quoted native
// coin. A successful preview is not a Privy permission or a live trade.
// No balances, wallet IDs, memos or signing bytes are persisted in the app.
export default function NativeSourceReadiness({
  request,enabled,userId,getAccessToken,t,
}) {
  const [status,setStatus]=useState(null);
  const [busy,setBusy]=useState(false);
  const [failed,setFailed]=useState(false);
  const ref=useRef({id:0,abort:null});
  const nativeRequest=nativeSourcePreflightRequest(request);
  useEffect(()=>{
    ref.current.id++;
    ref.current.abort?.abort();
    ref.current.abort=null;
    setStatus(null);setFailed(false);setBusy(false);
    return ()=>{
      ref.current.id++;
      ref.current.abort?.abort();
      ref.current.abort=null;
    };
  },[userId,enabled,request?.asset,request?.side,request?.stable,request?.amount]);
  if(!nativeRequest)return null;
  const check=async()=>{
    if(!enabled||busy||!getAccessToken)return;
    const id=++ref.current.id;
    ref.current.abort?.abort();
    const abort=new AbortController();
    ref.current.abort=abort;
    const timeout=setTimeout(()=>abort.abort(),30000);
    setBusy(true);setFailed(false);setStatus(null);
    try {
      const token=await getAccessToken();
      if(!token||abort.signal.aborted)throw Error('Privy authentication required');
      const response=await fetch(API_BASE+nativeRequest.path,{
        method:'POST',cache:'no-store',signal:abort.signal,
        headers:{
          Authorization:'Bearer '+token,Accept:'application/json',
          'Content-Type':'application/json',
        },
        body:JSON.stringify(nativeRequest.body),
      });
      if(!response.ok)throw Error('Original source preflight unavailable');
      const result=await response.json();
      if(!validNativeSourcePreflight(result,nativeRequest))
        throw Error('Invalid original source status');
      if(id===ref.current.id)
        setStatus(originalNativeBlocker(result,nativeRequest));
    }catch{
      if(id===ref.current.id)setFailed(true);
    }finally{
      clearTimeout(timeout);
      if(ref.current.abort===abort)ref.current.abort=null;
      if(id===ref.current.id)setBusy(false);
    }
  };
  const message=status==='NATIVE_WALLET_UNSUPPORTED'?t.nativeSourceUnsupported:
    status==='REMOTE_OWNER_UNVERIFIED'?t.nativeSourceOwnerMissing:
    status==='PUBLIC_KEY_UNVERIFIED'?t.nativeSourceKeyMissing:
    status==='SIGNER_NOT_ASSIGNED'?t.nativeSourceSignerMissing:
    status==='POLICY_AND_SEND_CLAIM_MISSING'?t.nativeSourcePolicyPending:
    t.nativeSourceUnavailable;
  return <section className="mininex-eth-policy-review">
    <button type="button"
      className="privy-wallet-action privy-wallet-action--secondary"
      disabled={!enabled||busy}
      onClick={check}>
      {busy?t.nativeSourceChecking:t.nativeSourceCheck}
    </button>
    {failed&&<p className="privy-wallet-description" role="alert">
      {t.nativeSourceUnavailable}
    </p>}
    {status&&<div className="mininex-swap-quote" role="status">
      <strong>{t.nativeSourceTitle} · {nativeRequest.asset}</strong>
      <p className="privy-wallet-description">{t.nativeSourceChecked}</p>
      <p className="privy-wallet-description">{message}</p>
      <p className="privy-wallet-description">{t.nativeSourceNotExecutable}</p>
    </div>}
  </section>;
}
