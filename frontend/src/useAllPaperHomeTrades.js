import {useEffect,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {HOME_PAPER_COINS,validHomePaperFeed} from './homePaperFeed.js';

/** The home timeline is the signed-in user's private saved PAPER ledger. */
export default function useAllPaperHomeTrades(paperVisible,homeVisible,refreshNonce,assetScope){
  const {ready,authenticated,user,getAccessToken}=usePrivy();
  const [state,setState]=useState({trades:[],status:'idle',scope:null});
  const did=user?.id||null;

  useEffect(()=>{
    let active=true;
    let inFlight=false;
    const controllers=new Set();
    setState({trades:[],status:'idle',scope:assetScope});
    if(!paperVisible||!homeVisible||!ready||!authenticated||!did)
      return()=>{active=false;};

    if(assetScope!=='ALL'&&!HOME_PAPER_COINS.includes(assetScope)){
      // ADA stays "In Arbeit"; no fake 404 or cross-coin history.
      setState({trades:[],status:'ready',scope:assetScope});
      return()=>{active=false;};
    }
    setState({trades:[],status:'loading',scope:assetScope});
    const sync=async()=>{
      if(!active||inFlight||document.hidden)return;
      inFlight=true;
      const controller=new AbortController();
      controllers.add(controller);
      try {
        const token=await getAccessToken();
        if(!active||controller.signal.aborted)return;
        if(!token)throw Error('Login required');
        const response=await fetch(API_BASE+'/api/home/paper-trades?asset='+encodeURIComponent(assetScope),{
          method:'GET',cache:'no-store',signal:controller.signal,
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        });
        if(!response.ok)throw Error('Private history unavailable');
        const data=await response.json();
        if(!validHomePaperFeed(data,assetScope))throw Error('Invalid PAPER-only history');
        if(active)setState({trades:data.trades,status:'ready',scope:assetScope});
      }catch{
        // Do not silently display old account data or an empty feed as
        // if it were a verified "no trades" response.
        if(active)setState({trades:[],status:'error',scope:assetScope});
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
  },[paperVisible,homeVisible,ready,authenticated,did,getAccessToken,refreshNonce,assetScope]);

  // Never flash the previously selected coin's trade history during a switch.
  return {...(state.scope===assetScope?state:{trades:[],status:'loading'}),
    authenticated:ready&&authenticated};
}
