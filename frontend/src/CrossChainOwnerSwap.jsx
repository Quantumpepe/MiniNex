import React,{useEffect,useRef,useState} from 'react';
import {useWallets} from '@privy-io/react-auth';
import {API_BASE,formatAssetQuantity} from './data.js';
import {notifyConfirmedWalletTransfer} from './walletBalanceRefresh.js';

const HASH=/^0x[0-9a-fA-F]{64}$/;
const ADDR=/^0x[0-9a-fA-F]{40}$/;
const DIAMOND='0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae';
const CHAINS={BNB:56,POL:137,AVAX:43114,ARB:42161};
const STABLE={USDC:'0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',
  USDT:'0xdac17f958d2ee523a2206206994597c13d831ec7'};
const SOURCE={BNB:['0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'],
  POL:['0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270'],
  AVAX:['0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7'],
  ARB:['0x912ce59144191c1204e64559fe8253a0e49e6548']};
const equal=(a,b)=>typeof a==='string'&&typeof b==='string'&&a.toLowerCase()===b.toLowerCase();
function units(raw,decimals){
  if(typeof raw!=='string'||!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(raw))throw Error('Invalid amount');
  const [w,f='']=raw.split('.');
  if(f.length>decimals)throw Error('Invalid amount precision');
  return BigInt(w)*10n**BigInt(decimals)+BigInt(f.padEnd(decimals,'0')||'0');
}

export function validateBridgePlan(plan,quote,asset,address){
  const data=quote.data,req=quote.request,buy=req.to===asset;
  const stable=buy?req.from:req.to,chain=buy?1:CHAINS[asset];
  const side=buy?'BUY':'SELL',amount=units(req.body.amount,buy?6:18);
  const tx=plan?.transaction;
  if(!tx||plan.mode!=='OWNER_MANUAL_ETHEREUM_SETTLEMENT'||plan.quote_id!==data.quote_id||
    plan.asset!==asset||plan.stable!==stable||plan.side!==side||
    plan.source_chain_id!==chain||!equal(plan.wallet_address,address)||
    !equal(tx.from,address)||plan.input_atomic!==amount.toString()||
    plan.minimum_output_atomic!==units(data.minimum_output_amount,buy?18:6).toString()||
    plan.expires_at!==data.quote_expires_at||Date.now()>=plan.expires_at*1000||
    plan.server_signing_authorized!==false||plan.automatic_trading_authorized!==false||
    !ADDR.test(tx.to)||typeof tx.data!=='string'||!/^0x(?:[0-9a-fA-F]{2})+$/.test(tx.data))
    throw Error('Original swap plan differs');
  const value=BigInt(tx.value);
  if(plan.kind==='APPROVAL'){
    if(value!==0n||!(buy?[STABLE[stable]]:SOURCE[asset]).includes(tx.to.toLowerCase())||
      !/^0x095ea7b3[0-9a-fA-F]{128}$/.test(tx.data)||
      tx.data.slice(10,74).toLowerCase()!==DIAMOND.slice(2).padStart(64,'0')||
      ![0n,amount].includes(BigInt('0x'+tx.data.slice(74))))throw Error('Approval differs');
  }else if(plan.kind==='SWAP'){
    if(!equal(tx.to,DIAMOND)||tx.chainId!==chain||
      (buy||asset==='ARB'?value!==0n:![0n,amount].includes(value)))
      throw Error('Bridge transaction differs');
  }else throw Error('Unsupported plan');
  return {from:address,to:tx.to,data:tx.data,value:'0x'+value.toString(16)};
}

export default function CrossChainOwnerSwap({asset,quote,enabled,userId,getAccessToken,lang,onRequote}){
  const {wallets}=useWallets();
  const wallet=wallets.find(w=>w?.walletClientType==='privy'&&ADDR.test(w?.address||''));
  const key='mininex_cross_chain_manual:'+userId+':'+asset+':'+(wallet?.address||'').toLowerCase();
  const [pending,setPending]=useState(null),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  const locked=useRef(false);
  const de=lang==='de';
  const words={waiting:de?'Tausch wird bestätigt …':'Swap is being confirmed…',
    unknown:de?'Übermittlung unklar. Nicht erneut senden.':'Submission uncertain. Do not send again.',
    approval:de?'Freigabe gesendet. Nach Bestätigung neuen Kurs holen.':'Approval sent. Refresh quote after confirmation.',
    expired:de?'Angebot abgelaufen oder Route nicht verfügbar.':'Quote expired or route unavailable.',
    done:de?'Tausch bestätigt':'Swap confirmed'};
  const save=p=>{localStorage.setItem(key,JSON.stringify(p));setPending(p);};
  const clear=()=>{localStorage.removeItem(key);setPending(null);};
  useEffect(()=>{setPending(null);setMessage('');if(!enabled||!wallet)return;
    try{const p=JSON.parse(localStorage.getItem(key)||'null');
      if(p?.quote_id&&p.asset===asset&&equal(p.wallet,wallet.address))setPending(p);
    }catch{/* Never interpret malformed storage as a transaction. */}
  },[key,enabled]);
  async function call(path,method='GET',body){
    const token=await getAccessToken();if(!token)throw Error('Login required');
    const r=await fetch(API_BASE+path,{method,cache:'no-store',headers:{
      Authorization:'Bearer '+token,Accept:'application/json','Content-Type':'application/json'},
      ...(body?{body:JSON.stringify(body)}:{})});
    if(!r.ok)throw Error('Swap service unavailable');return r.json();
  }
  const base='/api/wallet/swap/ethereum-settlement';
  const result=(id,state,hash=null)=>call(base+'/manual/'+id+'/result','POST',{state,tx_hash:hash});
  async function provider(chain){
    let p=await wallet.getEthereumProvider();
    if(BigInt(await p.request({method:'eth_chainId'}))!==BigInt(chain)){
      await wallet.switchChain(chain);p=await wallet.getEthereumProvider();
    }
    if(BigInt(await p.request({method:'eth_chainId'}))!==BigInt(chain)||
      !(await p.request({method:'eth_accounts'})).some(a=>equal(a,wallet.address)))
      throw Error('Wallet changed');
    return p;
  }
  async function send(){
    if(!quote||!wallet||!enabled||pending||locked.current||!quote.data.restricted_calldata_verified)return;
    locked.current=true;setBusy(true);setMessage('');
    let plan,attempt,started=false;
    try{
      const buy=quote.request.to===asset;
      const p=await provider(buy?1:CHAINS[asset]);
      plan=await call(base+'/manual/'+quote.data.quote_id+'/prepare','POST',{});
      const tx=validateBridgePlan(plan,quote,asset,wallet.address);
      if(plan.kind==='SWAP'){
        attempt={quote_id:plan.quote_id,asset,side:plan.side,stable:plan.stable,
          amount:quote.request.body.amount,wallet:wallet.address,hash:null};
        save(attempt); // Persist before the user opens the real Privy signature.
      }
      started=true;
      const hash=await p.request({method:'eth_sendTransaction',params:[tx]});
      if(!HASH.test(hash))throw Error('No transaction hash');
      if(plan.kind==='APPROVAL'){setMessage(words.approval);onRequote();return;}
      const sent={...attempt,hash:hash.toLowerCase()};save(sent);
      await result(sent.quote_id,'SUBMITTED',sent.hash);
      setMessage(words.waiting);
    }catch(e){
      if(attempt){
        if(started&&(e?.code===4001||e?.code==='ACTION_REJECTED')){
          try{await result(attempt.quote_id,'CANCELLED');clear();}catch{setMessage(words.unknown);}
        }else if(!started){
          try{await result(attempt.quote_id,'CANCELLED');clear();}catch{setMessage(words.unknown);}
        }else{
          try{await result(attempt.quote_id,'UNCERTAIN');}catch{}
          setMessage(words.unknown);
        }
      }else setMessage(started?words.unknown:words.expired);
    }finally{locked.current=false;setBusy(false);}
  }
  useEffect(()=>{
    if(!enabled||!pending?.hash)return;
    let active=true,timer;
    const check=async()=>{
      try{
        await result(pending.quote_id,'SUBMITTED',pending.hash);
        const q=new URLSearchParams({side:pending.side,stable:pending.stable,amount:pending.amount,quote_id:pending.quote_id});
        const s=await call(base+'/'+asset+'/'+pending.hash+'?'+q);
        if(!active)return;
        if(s.state==='FINALIZED'&&s.settlement_verified===true&&equal(s.source_tx_hash,pending.hash)){
          notifyConfirmedWalletTransfer(userId,asset,s.destination_tx_hash);
          setMessage(words.done+': '+formatAssetQuantity(s.received_amount,pending.side==='BUY'?asset:pending.stable,lang)+' '+(pending.side==='BUY'?asset:pending.stable));
          clear();return;
        }
        if(s.state==='SOURCE_REVERTED'&&equal(s.source_tx_hash,pending.hash)){
          clear();setMessage(de?'Transaktion fehlgeschlagen.':'Transaction reverted.');return;
        }
        setMessage(s.state==='REFUND_OR_PARTIAL'||s.state==='BELOW_QUOTED_MINIMUM'?words.unknown:words.waiting);
      }catch{if(active)setMessage(words.waiting);}
      if(active)timer=setTimeout(check,15000);
    };
    check();return()=>{active=false;clearTimeout(timer);};
  },[enabled,pending?.hash,key]);
  if(!pending&&!quote&&!message)return null;
  return <div className="mininex-swap-quote">
    {!pending&&quote&&<button className="privy-wallet-action" type="button"
      disabled={busy||!enabled||!wallet||quote.data.restricted_calldata_verified!==true}
      onClick={send}>{busy?(de?'Wird vorbereitet …':'Preparing…'):(de?'Swap bestätigen':'Confirm swap')}</button>}
    {pending&&!pending.hash&&<small role="status">{words.unknown}</small>}
    {message&&<small role="status">{message}</small>}
  </div>;
}
