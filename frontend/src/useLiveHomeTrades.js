import {useEffect,useRef,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {validOriginalNativeOrderHistory,nativeReceivedAmount} from './nativeLiveOrderHistoryRules.js';

const EVM_ASSETS=['BNB','POL','AVAX','LINK','QNT','UNI','ARB','ETH'];
const NATIVE_ASSETS=['BTC','SOL','XRP','TRX'];
const ALL=[...EVM_ASSETS,...NATIVE_ASSETS];
const amountOk=v=>typeof v==='string'&&/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(v)&&Number(v)>0;
function entry(row,asset){
  if(row?.asset!==asset||row.onchain_finalized!==true||
      !['BUY','SELL'].includes(row.side)||!['USDC','USDT'].includes(row.stable)||
      !amountOk(row.spent_amount)||!amountOk(row.received_amount)||
      row.spent_asset!==(row.side==='BUY'?row.stable:asset)||
      row.received_asset!==(row.side==='BUY'?asset:row.stable)||
      typeof row.tx_hash!=='string'||Number.isNaN(Date.parse(row.created_at)))return null;
  return {id:asset+':'+row.tx_hash,kind:row.side==='BUY'?'LIVE_BUY':'LIVE_SELL',
      amount_usd:null,settlement_amount:row.side==='BUY'?row.spent_amount:row.received_amount,
      settlement_asset:row.stable,price_reference:null,realized_net_profit:null,
      at:row.created_at,created_at:row.created_at,asset};
}
export default function useLiveHomeTrades(liveVisible,homeVisible,refreshNonce,asset,showAll=false){
  const {ready,authenticated,user,getAccessToken}=usePrivy();
  const tokenRef=useRef(getAccessToken);tokenRef.current=getAccessToken;
  const [state,setState]=useState({trades:[],status:'idle'});
  const did=user?.id||null;
  useEffect(()=>{
    let active=true;
    if(!liveVisible||!homeVisible||!ready||!authenticated||!did||!ALL.includes(asset)){
      setState({trades:[],status:'idle'});return;
    }
    const scope=showAll?ALL:[asset];
    setState({trades:[],status:'loading'});
    let pending=false;
    const sync=async()=>{
      if(pending)return;pending=true;
      try{
        const token=await tokenRef.current();if(!active||!token)return;
        const readRoute=async (coin,native)=>{
          const response=await fetch(API_BASE+(native?'/api/live/native/order-status/':'/api/live/evm/verified-fills/')+encodeURIComponent(coin)+'?limit=20',{
            method:'GET',cache:'no-store',headers:{Authorization:'Bearer '+token,Accept:'application/json'}
          });
          if(!response.ok)throw Error('live fills unavailable');
          const data=await response.json();
          if(native){
            if(!validOriginalNativeOrderHistory(data,coin))throw Error('native journal unavailable');
            return data.orders.filter(o=>o.destination_wallet_credited===true).map(o=>entry({
              asset:coin,side:o.side,stable:o.stable,onchain_finalized:true,
              spent_asset:o.original_input_asset,spent_amount:o.original_input_amount,
              received_asset:o.destination_asset,received_amount:nativeReceivedAmount(o),
              tx_hash:o.source_tx_hash,created_at:o.updated_at},coin)).filter(Boolean);
          }
          if(data?.asset!==coin||data.mode!=='FINALIZED_EVM_REAL_FILLS_ONLY'||!Array.isArray(data.fills))throw Error('unverified journal');
          return data.fills.map(row=>entry(row,coin)).filter(Boolean);
        };
        const read=async coin=>{
          if(!['BTC','XRP'].includes(coin))return readRoute(coin,NATIVE_ASSETS.includes(coin));
          // Preserve native historical transactions and also show the exact
          // newly finalized Ethereum trading inventory, without duplication.
          const both=await Promise.allSettled([readRoute(coin,true),readRoute(coin,false)]);
          const ok=both.filter(r=>r.status==='fulfilled');
          if(!ok.length)throw Error('live fills unavailable');
          return [...new Map(ok.flatMap(r=>r.value).map(row=>[row.id,row])).values()];
        };
        const results=await Promise.allSettled(scope.map(read));
        if(!active)return;
        const available=results.filter(r=>r.status==='fulfilled');
        const trades=available.flatMap(r=>r.value).sort((a,b)=>Date.parse(b.at)-Date.parse(a.at));
        setState({trades:trades.slice(0,60),status:available.length===scope.length?'ready':available.length?'partial':'error'});
      }catch{if(active)setState({trades:[],status:'error'});}finally{pending=false;}
    };
    void sync();
    const timer=setInterval(()=>{if(!document.hidden)void sync();},15000);
    return()=>{active=false;clearInterval(timer);};
  },[liveVisible,homeVisible,ready,authenticated,did,asset,showAll,refreshNonce]);
  return state;
}
