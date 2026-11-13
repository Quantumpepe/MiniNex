import {useEffect,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {validHomePaperFeed} from './homePaperFeed.js';

/** The home timeline is the signed-in user's private saved PAPER ledger. */
export default function useAllPaperHomeTrades(paperVisible,homeVisible,refreshNonce){
  const {ready,authenticated,user,getAccessToken}=usePrivy();
  const [state,setState]=useState({trades:[],status:'idle'});
  const did=user?.id||null;

  useEffect(()=>{
    let active=true;
    let inFlight=false;
    const controllers=new Set();
    setState({trades:[],status:'idle'});
    if(!paperVisible||!homeVisible||!ready||!authenticated||!did)
      return()=>{active=false;};

    setState({trades:[],status:'loading'});
    const sync=async()=>{
      if(!active||inFlight||document.hidden)return;
      inFlight=true;
      const controller=new AbortController();
      controllers.add(controller);
      try {
        const token=await getAccessToken();
        if(!active||controller.signal.aborted)return;
        if(!token)throw Error('Login required');
        const response=await fetch(API_BASE+'/api/home/paper-trades',{
          method:'GET',cache:'no-store',signal:controller.signal,
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        });
        if(!response.ok)throw Error('Private history unavailable');
        const data=await response.json();
        if(!validHomePaperFeed(data))throw Error('Invalid PAPER-only history');
        if(active)setState({trades:data.trades,status:'ready'});
      }catch{
        // Do not silently display old account data or an empty feed as
        // if it were a verified "no trades" response.
        if(active)setState({trades:[],status:'error'});
      }finally{
        controllers.delete(controller);
        inFlight=false;
      }
    };
    sync();
    const timer=window.setInterval(sync,20000);
    return()=>{
      active=false;
      window.clearInterval(timer);
      for(const controller of controllers)controller.abort();
      controllers.clear();
    };
  },[paperVisible,homeVisible,ready,authenticated,did,getAccessToken,refreshNonce]);

  return {...state,authenticated:ready&&authenticated};
}
