import {useEffect,useMemo,useRef,useState} from 'react';
import {readLiveEquity,appendLiveEquity,saveLiveEquity,losesWalletCoverage} from './livePortfolioHistory.js';
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
  const visibleRef=useRef(visible);
  visibleRef.current=visible;
  const syncRef=useRef(null);
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
    if(!owned)
      return ()=>{active=false;};
    const sync=async()=>{
      if(!active||inFlight||document.hidden||!visibleRef.current)return;
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
            if(!validate(data))return [key,null];
            // Publish verified initial sources immediately; do not wait for
            // the slowest network. Keep the last complete batch on refresh
            // until all sources have settled, avoiding mixed-time totals.
            if(active&&!controller.signal.aborted)setState(previous=>{
              if(previous.did!==did||previous.sources?.[key])return previous;
              return {did,sources:{...previous.sources,[key]:data},status:'refreshing'};
            });
            return [key,data];
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
    syncRef.current=sync;
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
      active=false;syncRef.current=null;unsubscribe();window.clearInterval(interval);
      window.removeEventListener('focus',refreshWhenVisible);
      document.removeEventListener('visibilitychange',refreshWhenVisible);
      for(const c of controllers)c.abort();
    };
  },[ready,authenticated,did,refreshNonce]);
  // Navigation must not abort an existing network request. Poll only when
  // the overview is visible, and resume immediately on return.
  useEffect(()=>{if(visible)void syncRef.current?.();},[visible]);
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
       summary.equity!==null){
      if(lastValued.did!==did||!losesWalletCoverage(lastValued.summary?.coverage,summary.coverage))
        setLastValued({did,summary});
    }
  },[matched,did,state.status,summary]);
  const savedPoint=matched&&samples.did===did?samples.points.at(-1):null;
  const remembered=matched&&lastValued.did===did?lastValued.summary:null;
  // Persisted observations are aggregate USD values only. Never invent
  // quantities or treat a previous observation as a fresh wallet read.
  const historical=savedPoint?{
    equity:Math.round(savedPoint.value*100)/100,valuationAvailable:true,
    incomplete:!savedPoint.coverage.split('|').every(part=>/:(full|absent)$/.test(part)),
    holdings:[],cached:true,mode:'LIVE_WALLET',currency:'USD',coverage:savedPoint.coverage,
  }:null;
  const fallback=remembered?{...remembered,cached:true}:historical;
  const currentComplete=summary?.valuationAvailable&&!summary.incomplete;
  const displayedSummary=currentComplete
    ?{...summary,cached:state.status==='stale'||state.status==='partial'}
    :fallback&&(!fallback.incomplete||losesWalletCoverage(fallback.coverage,summary?.coverage))
      ?{...fallback,missingCoverage:summary?.incomplete?summary.coverage:null}:
      summary?.valuationAvailable?{...summary,cached:state.status==='stale'||state.status==='partial'}:
      fallback||summary;
  useEffect(()=>{
    // The chart consumes the same settled USD total as the portfolio card.
    // Do not fabricate observations when the card shows a cached amount.
    if(!visible||!matched||state.status!=='ready'||displayedSummary?.cached||
       !displayedSummary?.valuationAvailable||!Number.isFinite(displayedSummary.equity))return;
    setSamples(prev=>{
      const points=prev.did===did?prev.points:readLiveEquity(did);
      const next=appendLiveEquity(points,{...displayedSummary,observedEquityUsd:displayedSummary.equity});
      return next===points?prev:{did,points:next};
    });
  },[visible,matched,did,state.status,displayedSummary?.equity,displayedSummary?.valuationAvailable,displayedSummary?.cached,syncedAt]);
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
