import {useEffect,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE} from './data.js';

const EVM_ASSETS=['BNB','POL','AVAX','LINK','UNI','ARB','ETH'];

/** Fetches verified live fills for the selected asset when simulation is off. */
export default function useLiveHomeTrades(liveVisible,homeVisible,refreshNonce,asset){
  const {ready,authenticated,user,getAccessToken}=usePrivy();
  const [state,setState]=useState({trades:[],status:'idle'});
  const did=user?.id||null;

  useEffect(()=>{
    let active=true;
    if(!liveVisible||!homeVisible||!ready||!authenticated||!did||!EVM_ASSETS.includes(asset)){
      setState({trades:[],status:'idle'});
      return;
    }
    setState({trades:[],status:'loading'});
    const sync=async()=>{
      try{
        const token=await getAccessToken();
        if(!active||!token)return;
        const response=await fetch(API_BASE+'/api/live/evm/verified-fills/'+encodeURIComponent(asset)+'?limit=20',{
          method:'GET',cache:'no-store',
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        });
        if(!response.ok)throw Error('live fills unavailable');
        const data=await response.json();
        if(!active)return;
        const rows=Array.isArray(data?.fills)?data.fills:Array.isArray(data)?data:[];
        const trades=rows.map(row=>({
          id:row.tx_hash||row.id||Math.random().toString(36).slice(2),
          kind:row.side==='SELL'?'LIVE_SELL':'LIVE_BUY',
          amount_usd:row.amount_usd||row.notional_usd||null,
          price:row.price||null,
          realized_net_profit:row.realized_pnl||null,
          created_at:row.created_at||row.filled_at||null,
          asset:row.asset||asset,
        }));
        setState({trades,status:'ready'});
      }catch{
        if(active)setState({trades:[],status:'error'});
      }
    };
    sync();
    const timer=setInterval(()=>{if(!document.hidden)sync();},15000);
    return()=>{active=false;clearInterval(timer);};
  },[liveVisible,homeVisible,ready,authenticated,did,asset,refreshNonce,getAccessToken]);

  return state;
}
