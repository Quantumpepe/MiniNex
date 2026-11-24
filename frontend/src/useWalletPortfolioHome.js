import {useEffect,useMemo,useRef,useState} from 'react';
import {readLiveEquity,appendLiveEquity,saveLiveEquity} from './livePortfolioHistory.js';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {onConfirmedWalletTransfer} from './walletBalanceRefresh.js';
import {summarizeVerifiedWalletPortfolio} from './walletPortfolio.js';
import {validateWalletBalanceResponse} from './walletBalanceValidation.js';
import {validateMulticoinBalanceResponse} from './multicoinBalanceValidation.js';
import {validNativeBalanceResponse} from './nativeBalanceValidation.js';

const SOURCES=[
  ['core','/api/wallet/balances',validateWalletBalanceResponse],
  ['evm','/api/wallet/multicoin-balances',validateMulticoinBalanceResponse],
  ['native','/api/wallet/native-balances',validNativeBalanceResponse],
];

export default function useWalletPortfolioHome(visible,refreshNonce,markets,syncedAt){
  const {ready,authenticated,user,getAccessToken}=usePrivy();
  const did=user?.id||null;
  const tokenRef=useRef(getAccessToken);
  tokenRef.current=getAccessToken;
  const [state,setState]=useState({did:null,sources:null,status:'idle'});
  const [samples,setSamples]=useState({did:null,points:[]});
  const [lastValued,setLastValued]=useState({did:null,summary:null});
  useEffect(()=>{
    if(!ready||!authenticated||!did)
      setLastValued(previous=>previous.did===null?previous:
        {did:null,summary:null});
  },[ready,authenticated,did]);
  useEffect(()=>{
    let active=true,inFlight=false,refreshQueued=false;
    const controllers=new Set();
    // Keep the LAST independently verified wallet data when navigation hides
    // the overview or a refresh begins. A page change is not a logout.
    // A different Privy DID, however, must never inherit old wallet values.
    const owned=ready&&authenticated&&typeof did==='string'&&
      did.startsWith('did:privy:');
    setState(previous=>{
      if(!owned)return previous.did===null&&!previous.sources?previous:
        {did:null,sources:null,status:'idle'};
      if(previous.did!==did)return {did,sources:null,
        status:visible?'loading':'idle'};
      if(!visible)return previous;
      return {...previous,status:previous.sources?'refreshing':'loading'};
    });
    setSamples(previous=>
      owned&&previous.did===did?previous:{did:owned?did:null,points:owned?readLiveEquity(did):[]});
    if(!visible||!owned)
      return ()=>{active=false;};
    const sync=async()=>{
      if(!active||inFlight||document.hidden)return;
      inFlight=true;
      const controller=new AbortController();
      controllers.add(controller);
      try{
        const token=await tokenRef.current();
        if(!token)throw new Error('Authentication required');
        const values=await Promise.all(SOURCES.map(async ([key,path,validate])=>{
          try{
            const response=await fetch(API_BASE+path,{
              method:'GET',cache:'no-store',signal:controller.signal,
              headers:{Authorization:'Bearer '+token,Accept:'application/json'},
            });
            if(!response.ok)throw new Error('Wallet source unavailable');
            const data=await response.json();
            return [key,validate(data)?data:null];
          }catch{return [key,null];}
        }));
        if(!active||controller.signal.aborted)return;
        const verified=Object.fromEntries(values.filter(([,data])=>data));
        setState(previous=>{
          if(previous.did!==did)return previous;
          if(!Object.keys(verified).length){
            // A transient RPC/network failure does not turn an already
            // verified balance into zero or erase the observed chart.
            return {...previous,status:previous.sources?'stale':'error'};
          }
          return {did,sources:{...previous.sources,...verified},
            status:Object.keys(verified).length===SOURCES.length?'ready':'partial'};
        });
      }catch{
        if(active&&!controller.signal.aborted)
          setState(previous=>previous.did===did
            ?{...previous,status:previous.sources?'stale':'error'}:previous);
      }finally{
        inFlight=false;controllers.delete(controller);
        if(active&&refreshQueued){refreshQueued=false;void sync();}
      }
    };
    sync();
    // An outgoing transfer may finish while the overview is open. Update
    // the total portfolio immediately rather than waiting for the next poll.
    const unsubscribe=onConfirmedWalletTransfer(did,()=>{
      if(inFlight)refreshQueued=true;
      else void sync();
    });
    const refreshWhenVisible=()=>{if(!document.hidden)void sync();};
    window.addEventListener('focus',refreshWhenVisible);
    document.addEventListener('visibilitychange',refreshWhenVisible);
    const interval=window.setInterval(sync,20000);
    return ()=>{
      active=false;unsubscribe();window.clearInterval(interval);
      window.removeEventListener('focus',refreshWhenVisible);
      document.removeEventListener('visibilitychange',refreshWhenVisible);
      for(const c of controllers)c.abort();
    };
  },[visible,ready,authenticated,did,refreshNonce]);
  const matched=state.did===did&&ready&&authenticated;
  const summary=useMemo(()=>matched? summarizeVerifiedWalletPortfolio(
    state.sources,markets,syncedAt):null,
    [matched,state.sources,markets,syncedAt]);
  // Exchange prices can briefly age out when the user returns from the
  // wallet screen. Keep the last fully observable wallet valuation for the
  // SAME Privy account rather than flashing an empty card. New prices and
  // holdings supersede it only after independent verification.
  useEffect(()=>{
    if(matched&&state.status==='ready'&&summary?.valuationAvailable&&
       summary.equity!==null)
      setLastValued({did,summary});
  },[matched,did,state.status,summary]);
  const displayedSummary=summary?.valuationAvailable?summary:
    matched&&lastValued.did===did&&lastValued.summary
      ?{...lastValued.summary,incomplete:true}:summary;
  useEffect(()=>{
    if(!visible||!matched||!['ready','partial'].includes(state.status)||
       !summary?.valuationAvailable||summary.equity===null)return;
    setSamples(prev=>{
      const points=prev.did===did?prev.points:readLiveEquity(did);
      const next=appendLiveEquity(points,summary);
      return next===points?prev:{did,points:next};
    });
  },[visible,matched,did,state.status,summary?.observedEquityUsd,summary?.valuationAvailable,summary?.coverage,syncedAt]);
  useEffect(()=>{
    if(matched&&samples.did===did&&samples.points.length)
      saveLiveEquity(did,samples.points);
  },[matched,did,samples]);
  return {
    summary:displayedSummary,samples:matched&&samples.did===did?samples.points:[],
    status:matched?state.status:'loading',
    authenticated:ready&&authenticated,
  };
}
