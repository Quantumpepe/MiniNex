import {useEffect,useMemo,useState} from 'react';
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
  const [state,setState]=useState({did:null,sources:null,status:'idle'});
  const [samples,setSamples]=useState({did:null,points:[]});
  useEffect(()=>{
    let active=true,inFlight=false,refreshQueued=false;
    const controllers=new Set();
    setState({did,sources:null,status:'loading'});
    setSamples({did,points:[]});
    if(!visible||!ready||!authenticated||!did)
      return ()=>{active=false;};
    const sync=async()=>{
      if(!active||inFlight||document.hidden)return;
      inFlight=true;
      const controller=new AbortController();
      controllers.add(controller);
      try{
        const token=await getAccessToken();
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
        const sources=Object.fromEntries(values);
        setState({did,sources,status:Object.values(sources).some(Boolean)?'ready':'error'});
      }catch{
        if(active&&!controller.signal.aborted)
          setState({did,sources:null,status:'error'});
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
    const interval=window.setInterval(sync,20000);
    return ()=>{active=false;unsubscribe();window.clearInterval(interval);for(const c of controllers)c.abort();};
  },[visible,ready,authenticated,did,getAccessToken,refreshNonce]);
  const matched=state.did===did&&ready&&authenticated;
  const summary=useMemo(()=>matched? summarizeVerifiedWalletPortfolio(
    state.sources,markets,syncedAt):null,
    [matched,state.sources,markets,syncedAt]);
  useEffect(()=>{
    if(!matched||!summary?.valuationAvailable||summary.equity===null)return;
    setSamples(prev=>{
      const points=prev.did===did?prev.points:[];
      const now=Date.now();
      if(points.length&&now-points[points.length-1].at<12000)return prev;
      return {did,points:[...points,{at:now,value:summary.equity}].slice(-90)};
    });
  },[matched,did,summary?.equity,summary?.valuationAvailable]);
  return {
    summary,samples:samples.did===did?samples.points:[],
    status:matched?state.status:'loading',
    authenticated:ready&&authenticated,
  };
}
