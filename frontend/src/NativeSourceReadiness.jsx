import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {nativeSourcePreflightRequest,validNativeSourcePreflight,
  originalNativeBlocker,validNativeOwnerReadback,
  nativeOwnerReadbackBlocker} from './nativeSourceReadinessRules.js';

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
    let token=null;
    try {
      token=await getAccessToken();
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
        setStatus({code:originalNativeBlocker(result,nativeRequest),
          sourceChecked:true});
    }catch{
      // A failed chain read does not prove that the user's wallet is
      // absent. Fetch the separate Privy owner status, without suggesting
      // that any source fee, balance or source transaction was verified.
      let ownerCode=null;
      if(token&&!abort.signal.aborted){
        try {
          const response=await fetch(
            API_BASE+'/api/live/native/authorization/'+nativeRequest.asset,{
              method:'GET',cache:'no-store',signal:abort.signal,
              headers:{Authorization:'Bearer '+token,Accept:'application/json'},
            },
          );
          if(response.ok){
            const owner=await response.json();
            if(validNativeOwnerReadback(owner,nativeRequest.asset)){
              ownerCode=nativeOwnerReadbackBlocker(owner,nativeRequest.asset);
            }
          }
        }catch{
          // No reliable owner result: report an unavailable source.
        }
      }
      if(id===ref.current.id){
        if(ownerCode&&ownerCode!=='INVALID')
          setStatus({code:ownerCode,sourceChecked:false});
        else setFailed(true);
      }
    }finally{
      clearTimeout(timeout);
      if(ref.current.abort===abort)ref.current.abort=null;
      if(id===ref.current.id)setBusy(false);
    }
  };
  const code=status?.code;
  const message=code==='NATIVE_WALLET_UNSUPPORTED'?t.nativeSourceUnsupported:
    code==='ORIGINAL_WALLET_MISSING'?t.nativeSourceWalletMissing:
    code==='REMOTE_OWNER_UNVERIFIED'?t.nativeSourceOwnerMissing:
    code==='PUBLIC_KEY_UNVERIFIED'?t.nativeSourceKeyMissing:
    code==='SIGNER_NOT_ASSIGNED'?t.nativeSourceSignerMissing:
    code==='POLICY_AND_SEND_CLAIM_MISSING'?t.nativeSourcePolicyPending:
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
      <p className="privy-wallet-description">
        {status.sourceChecked?t.nativeSourceChecked:t.nativeSourceNotChecked}
      </p>
      <p className="privy-wallet-description">{message}</p>
      <p className="privy-wallet-description">{t.nativeSourceNotExecutable}</p>
    </div>}
  </section>;
}
