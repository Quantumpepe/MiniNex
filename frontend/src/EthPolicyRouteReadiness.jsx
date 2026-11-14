import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {
  validEthPolicyQuote,validEthPolicyAllowance,checkEthPolicyStatus,
} from './ethPolicyReadinessRules.js';

// Uses the current ETH quote's SAME side, stable and amount; no duplicate
// trading settings and NO Privy delegated signer or permission changes.
export default function EthPolicyRouteReadiness({
  request,enabled,getAccessToken,t,userId,
}) {
  const [state,setState]=useState(null);
  const [busy,setBusy]=useState(false);
  const [failed,setFailed]=useState(false);
  const ref=useRef({id:0,abort:null});
  useEffect(()=>{
    ref.current.id++;
    ref.current.abort?.abort();
    ref.current.abort=null;
    setState(null);setFailed(false);setBusy(false);
    return()=>{ref.current.id++;ref.current.abort?.abort();};
  },[userId,enabled,request?.side,request?.stable,request?.amount]);

  if(request?.asset!=='ETH')return null;
  const check=async()=>{
    if(!request||!enabled||busy||!getAccessToken)return;
    const id=++ref.current.id;
    ref.current.abort?.abort();
    const abort=new AbortController();
    ref.current.abort=abort;
    const timeout=setTimeout(()=>abort.abort(),15000);
    setBusy(true);setFailed(false);setState(null);
    try {
      const token=await getAccessToken();
      if(!token||abort.signal.aborted)throw Error('Not logged in');
      const paths=[
        '/api/live/eth/router02-quote',
        '/api/live/eth/router02-allowance',
      ];
      const replies=await Promise.all(paths.map(path=>fetch(API_BASE+path,{
        method:'POST',cache:'no-store',signal:abort.signal,
        headers:{Authorization:'Bearer '+token,Accept:'application/json',
          'Content-Type':'application/json'},
        body:JSON.stringify({
          side:request.side,stable:request.stable,amount:request.amount,
        }),
      })));
      if(replies.some(r=>!r.ok))throw Error('Onchain review unavailable');
      const [route,allowance]=await Promise.all(replies.map(r=>r.json()));
      if(!validEthPolicyQuote(route,request)||
         !validEthPolicyAllowance(allowance,request))
        throw Error('Unrecognized onchain review');
      const outcome=checkEthPolicyStatus(route,allowance,request);
      if(id===ref.current.id)setState({route,allowance,outcome});
    }catch{
      if(id===ref.current.id)setFailed(true);
    }finally{
      clearTimeout(timeout);
      if(ref.current.abort===abort)ref.current.abort=null;
      if(id===ref.current.id)setBusy(false);
    }
  };
  const status=state?.outcome;
  return <section className="mininex-eth-policy-review">
    <button type="button"
      className="privy-wallet-action privy-wallet-action--secondary"
      disabled={!enabled||busy||!request}
      onClick={check}>
      {busy?t.livePlanLoading:t.ethRouteReadinessButton}
    </button>
    {failed&&<p className="privy-wallet-description" role="alert">
      {t.ethRouteReadinessUnavailable}
    </p>}
    {state&&<div className="mininex-swap-quote" role="status">
      <strong>{t.ethRouteReadinessTitle}</strong>
      <p className="privy-wallet-description">
        {t.ethRouteReadinessSwap}: {state.route.expected_swap_output_amount}
        {' '}{state.route.swap_output_token}
      </p>
      <p className="privy-wallet-description">
        {t.ethRouteReadinessBalance}: {state.allowance.verified_source_balance}
        {' '}{state.allowance.source_token}
      </p>
      <p className="privy-wallet-description">
        {t.ethRouteReadinessAllowance}: {state.allowance.existing_router_allowance}
        {' '}{state.allowance.source_token}
      </p>
      <p className="privy-wallet-description">{
        status==='QUOTE_EXPIRED'?t.ethRouteReadinessExpired:
        status==='SOURCE_BALANCE_MISSING'?t.ethRouteReadinessFunds:
        status==='ALLOWANCE_MISSING'?t.ethRouteReadinessApproval:
        status==='GAS_RESERVE_MISSING'?t.ethRouteReadinessGas:
        status==='PREVIEW_ONLY'?t.ethRouteReadinessFound:
        t.ethRouteReadinessUnavailable
      }</p>
      <p className="privy-wallet-description">{t.ethRouteReadinessCaveat}</p>
    </div>}
  </section>;
}
