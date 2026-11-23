import React,{useEffect,useState} from 'react';
import {API_BASE} from './data.js';
import {notifyConfirmedWalletTransfer} from './walletBalanceRefresh.js';

// Shared minimal user-facing confirmation for manual coin transfers.
// A network submission/signature is not a completed transfer.
const BASE58='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
export function solanaSignatureBase58(value){
  if(typeof value==='string')return /^[1-9A-HJ-NP-Za-km-z]{80,90}$/.test(value)?value:null;
  if(!(value instanceof Uint8Array)||value.length!==64)return null;
  let n=BigInt('0x'+Array.from(value,b=>b.toString(16).padStart(2,'0')).join(''));
  let out='';
  while(n>0n){out=BASE58[Number(n%58n)]+out;n/=58n;}
  for(const byte of value){if(byte!==0)break;out='1'+out;}
  return out;
}

async function confirmedTransfer(asset,txid,token,signal){
  if(asset==='BTC'){
    const response=await fetch('https://mempool.space/api/tx/'+encodeURIComponent(txid),{signal,cache:'no-store'});
    if(!response.ok)return false;
    const data=await response.json();
    return data?.txid===txid && data?.status?.confirmed===true;
  }
  if(asset==='XRP'||asset==='SOL'){
    if(!token)return false;
    const response=await fetch(API_BASE+'/api/live/native/source-receipt/'+asset+'/'+encodeURIComponent(txid),{
      headers:{Authorization:'Bearer '+token},signal,cache:'no-store',
    });
    if(!response.ok)return false;
    const data=await response.json();
    return data?.source_tx_hash===txid && data?.source_chain_finalized===true &&
      (asset==='XRP'?data?.source_wallet_transaction_verified===true:
        data?.source_wallet_signature_verified===true);
  }
  if(asset==='TRX'){
    if(!token||!/^[a-f0-9]{64}$/i.test(txid))return false;
    const response=await fetch(API_BASE+'/api/wallet/send/trx/receipt/'+encodeURIComponent(txid),{
      headers:{Authorization:'Bearer '+token},cache:'no-store',signal,
    });
    if(!response.ok)return false;
    const data=await response.json();
    return data?.confirmed===true;
  }
  return false;
}

export default function SendCompletion({asset,txid,t,userId,getAccessToken}){
  const [confirmed,setConfirmed]=useState(false);
  useEffect(()=>{
    let active=true;
    let timer;
    let abort;
    let attempts=0;
    setConfirmed(false);
    if(!txid||!userId)return()=>{active=false;};
    const check=async()=>{
      if(!active)return;
      attempts+=1;
      abort=new AbortController();
      try{
        const token=['XRP','SOL','TRX'].includes(asset)?await getAccessToken?.():null;
        const ok=await confirmedTransfer(asset,txid,token,abort.signal);
        if(!active)return;
        if(ok){
          setConfirmed(true);
          notifyConfirmedWalletTransfer(userId,asset,txid);
          return;
        }
      }catch{/* A provider delay is not proof of failure or success. */}
      if(active&&attempts<25)timer=setTimeout(check,5000);
    };
    void check();
    return()=>{active=false;abort?.abort();clearTimeout(timer);};
  },[asset,txid,userId,getAccessToken]);
  if(!txid)return null;
  return <p role="status" className="privy-wallet-description">
    {confirmed?(t.walletSendDone||'Done'):(t.walletSendPending||'Confirming…')}
  </p>;
}
