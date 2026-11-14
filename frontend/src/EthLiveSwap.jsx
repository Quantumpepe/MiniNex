import React,{useEffect,useRef,useState} from 'react';
import {useWallets} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {ROUTER02,abiAllowance,abiBalance,abiApprove,decodeUint,
  ownerAccounts,validHash,validateOwnerRouter02Call} from './ethLiveSwapRules.js';

// A REAL, user-initiated ERC20/WETH swap using the existing embedded Privy wallet.
// The separate native ETH WETH9 conversion is NOT yet automated or represented as settled.
export default function EthLiveSwap({enabled,userId,getAccessToken,side,stable,amount,t}){
  const {wallets}=useWallets();
  const [review,setReview]=useState(null);
  const [stage,setStage]=useState('idle');
  const [error,setError]=useState('');
  const [hash,setHash]=useState('');
  const [busy,setBusy]=useState(false);
  const serial=useRef(0);
  const attempted=useRef(false);
  const provider=useRef(null);
  const wallet=wallets.find(w=>w?.walletClientType==='privy'&&
    /^0x[0-9a-fA-F]{40}$/.test(w?.address||''));
  useEffect(()=>{
    serial.current++;setReview(null);setStage('idle');
    setError('');setHash('');setBusy(false);
    provider.current=null;attempted.current=false;
  },[enabled,userId,side,stable,amount]);

  async function getProvider(){
    if(!wallet||typeof wallet.getEthereumProvider!=='function')throw Error('wallet');
    const p=await wallet.getEthereumProvider();
    if(!p||typeof p.request!=='function')throw Error('provider');
    if(BigInt(await p.request({method:'eth_chainId'}))!==1n)
      throw Error('Ethereum mainnet wallet required');
    const accounts=await p.request({method:'eth_accounts'});
    if(!ownerAccounts(accounts,wallet.address))throw Error('different wallet');
    return p;
  }

  async function prepare(){
    if(!enabled||busy||attempted.current||!getAccessToken||!wallet)return;
    const id=++serial.current;
    setBusy(true);setError('');setReview(null);setStage('preparing');
    try{
      const token=await getAccessToken();
      if(!token)throw Error('login');
      const response=await fetch(API_BASE+'/api/live/eth/router02-user-transaction',{
        method:'POST',cache:'no-store',
        headers:{Authorization:'Bearer '+token,Accept:'application/json',
          'Content-Type':'application/json'},
        body:JSON.stringify({side,stable,amount})});
      if(!response.ok)throw Error('quote');
      const quote=await response.json();
      const tx=validateOwnerRouter02Call(quote,{
        wallet:wallet.address,side,stable,amount});
      const p=await getProvider();
      const holding=decodeUint(await p.request({method:'eth_call',
        params:[{to:tx.sourceToken,data:abiBalance(wallet.address)},'latest']}));
      if(holding<tx.amountAtomic)
        throw Error(side==='SELL'?'WETH required before selling':'Source token balance too low');
      const allowance=decodeUint(await p.request({method:'eth_call',
        params:[{to:tx.sourceToken,data:abiAllowance(wallet.address,ROUTER02)},'latest']}));
      const next=allowance<tx.amountAtomic?'approval':'ready';
      if(id!==serial.current)return;
      provider.current=p;setReview(tx);setStage(next);
    }catch(e){if(id===serial.current){setStage('failed');setError(e?.message||'Unavailable');}}
    finally{if(id===serial.current)setBusy(false);}
  }

  async function submit(kind){
    if(!enabled||busy||attempted.current||!review||!provider.current||
      !['approval','ready'].includes(stage)||
      (kind==='approval')!==(stage==='approval'))return;
    const id=serial.current;
    if(Date.now()>=review.expiresAt){setReview(null);setStage('expired');return;}
    attempted.current=true;setBusy(true);setError('');setStage('sending');
    try{
      const p=await getProvider();
      const tx=kind==='approval'?{
        from:wallet.address,to:review.sourceToken,value:'0x0',
        data:abiApprove(ROUTER02,review.amountAtomic)}:{
          from:review.from,to:review.to,data:review.data,value:'0x0'};
      // Explicit user tap, delegated AUTO never used here.
      const result=await p.request({method:'eth_sendTransaction',params:[tx]});
      if(!validHash(result))throw Error('Transaction hash uncertain; do not retry');
      if(id!==serial.current)return;
      setHash(result);setStage(kind==='approval'?'approval-submitted':'swap-submitted');
    }catch(e){
      if(id===serial.current){setStage('uncertain');
        setError('Transaction was rejected or may have been broadcast. Check your wallet before retrying.');}
    }finally{if(id===serial.current)setBusy(false);}
  }

  async function receipt(){
    if(!hash||busy||!provider.current)return;
    const id=serial.current;setBusy(true);
    try{
      const p=provider.current;
      const found=await p.request({method:'eth_getTransactionReceipt',params:[hash]});
      if(id!==serial.current)return;
      if(!found){setError('Not confirmed yet');return;}
      if(found.transactionHash?.toLowerCase()!==hash.toLowerCase()||
         found.from?.toLowerCase()!==wallet.address.toLowerCase()||
         found.status!=='0x1')throw Error('Reverted or untrusted receipt');
      if(stage==='approval-submitted')attempted.current=false;
      setStage(stage==='approval-submitted'?'approval-confirmed':'swap-confirmed');
      setReview(null);
    }catch(e){if(id===serial.current)setError(e?.message||'Receipt unavailable');}
    finally{if(id===serial.current)setBusy(false);}
  }

  return <div className="mininex-swap-quote" aria-label="ETH Privy real swap">
    <strong>{t.liveSwapTitle}</strong>
    <p className="privy-wallet-description">{t.liveSwapNote}</p>
    {!attempted.current&&['idle','failed','expired','approval-confirmed'].includes(stage)&&
      <button type="button" className="privy-wallet-action"
        disabled={!enabled||busy||!wallet||!amount} onClick={prepare}>
        {busy?'…':t.liveSwapPrepare}</button>}
    {stage==='approval'&&<button type="button" className="privy-wallet-action"
      disabled={busy||Date.now()>=review?.expiresAt} onClick={()=>submit('approval')}>
      {t.liveSwapApprove}</button>}
    {stage==='ready'&&<button type="button" className="privy-wallet-action"
      disabled={busy||Date.now()>=review?.expiresAt} onClick={()=>submit('swap')}>
      {t.liveSwapSign}</button>}
    {hash&&<div><code>{hash}</code>
      <a target="_blank" rel="noopener noreferrer"
        href={'https://etherscan.io/tx/'+hash}>{t.liveSwapExplorer}</a></div>}
    {['approval-submitted','swap-submitted'].includes(stage)&&
      <button type="button" className="privy-wallet-action"
        disabled={busy} onClick={receipt}>{t.liveSwapCheckReceipt}</button>}
    {stage==='approval-confirmed'&&<p>{t.liveSwapApprovalConfirmed}</p>}
    {stage==='swap-confirmed'&&<p>{t.liveSwapConfirmed}</p>}
    {error&&<p role="alert">{error}</p>}
    {busy&&<p role="status">{t.liveSwapWaiting}</p>}
  </div>;
}
