import React,{useEffect,useRef,useState} from 'react';
import {useWallets} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {ROUTER02,WETH9,abiAllowance,abiBalance,abiApprove,decodeUint,
  exactEthUnits,wethWithdrawCalldata,
  ownerAccounts,validHash,validateOwnerRouter02Call} from './ethLiveSwapRules.js';

// A REAL, user-initiated ERC20/WETH swap using the existing embedded Privy wallet.
// The separate native ETH WETH9 conversion is NOT yet automated or represented as settled.
export default function EthLiveSwap({enabled,userId,getAccessToken,side,stable,amount,t,onTradePendingChange}){
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
      // Native ETH SELL begins with the actual user-signed WETH9 deposit.
      // No quote is requested before the source WETH has been funded.
      if(side==='SELL'){
        const p=await getProvider();
        const required=exactEthUnits(amount);
        const holding=decodeUint(await p.request({method:'eth_call',
          params:[{to:WETH9,data:abiBalance(wallet.address)},'latest']}));
        if(holding<required){
          const remaining=required-holding;
          const native=BigInt(await p.request({method:'eth_getBalance',
            params:[wallet.address,'latest']}));
          if(native<=remaining+5000000000000000n)
            throw Error('Insufficient ETH to wrap and pay gas');
          if(id!==serial.current)return;
          provider.current=p;
          setReview({from:wallet.address,to:WETH9,data:'0xd0e30db0',
            value:'0x'+remaining.toString(16),amountAtomic:remaining,
            expiresAt:Date.now()+60000});
          setStage('wrap-required');
          return;
        }
      }
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
    const permitted={
      approval:'approval',swap:'ready',
      wrap:'wrap-required',unwrap:'unwrap-ready',
    };
    if(!enabled||busy||attempted.current||!review||!provider.current||
      permitted[kind]!==stage)return;
    const id=serial.current;
    if(Date.now()>=review.expiresAt){setReview(null);setStage('expired');return;}
    attempted.current=true;onTradePendingChange?.(true);
    setBusy(true);setError('');setStage('sending');
    try{
      const p=await getProvider();
      const tx=kind==='approval'?{
        from:wallet.address,to:review.sourceToken,value:'0x0',
        data:abiApprove(ROUTER02,review.amountAtomic)
      }:kind==='wrap'?{
        from:wallet.address,to:WETH9,value:review.value,data:'0xd0e30db0'
      }:kind==='unwrap'?{
        from:wallet.address,to:WETH9,value:'0x0',
        data:wethWithdrawCalldata(review.amountAtomic)
      }:{from:review.from,to:review.to,data:review.data,value:'0x0'};
      // Explicit user tap, delegated AUTO never used here.
      const result=await p.request({method:'eth_sendTransaction',params:[tx]});
      if(!validHash(result))throw Error('Transaction hash uncertain; do not retry');
      if(id!==serial.current)return;
      setHash(result);setStage(kind+'-submitted');
    }catch(e){
      if(id===serial.current){
        if(e?.code===4001||e?.code==='ACTION_REJECTED'){
          attempted.current=false;onTradePendingChange?.(false);
          setReview(null);setStage('idle');
          setError('Privy signing cancelled; nothing was broadcast.');
        }else{
          setStage('uncertain');
          setError('Transaction may have been broadcast. Check your wallet before retrying.');
        }
      }
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
      if(stage==='approval-submitted'||stage==='wrap-submitted'){
        attempted.current=false;onTradePendingChange?.(false);
        setStage(stage==='approval-submitted'?'approval-confirmed':'wrap-confirmed');
        setReview(null);return;
      }
      if(stage==='swap-submitted'||stage==='unwrap-submitted'){
        const token=await getAccessToken();
        if(!token)throw Error('Login expired');
        const route=stage==='swap-submitted'?
          '/api/live/eth/router02-receipt/'+hash+
            '?side='+encodeURIComponent(side)+'&stable='+encodeURIComponent(stable):
          '/api/live/eth/weth9-receipt/'+hash+'?operation=WITHDRAW';
        const response=await fetch(API_BASE+route,{
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
          cache:'no-store'});
        if(!response.ok)throw Error('On-chain proof not yet available');
        const body=await response.json();
        if(stage==='unwrap-submitted'){
          if(body.state!=='FINALIZED_WETH9_CONVERSION'||
             body.confirmed_native_conversion!==true){
            setError('Native ETH conversion is not finalized');return;
          }
          onTradePendingChange?.(false);
          setStage('native-complete');setReview(null);return;
        }
        if(body.state!=='FINALIZED_ERC20_SWAP_LEG'||
           body.swap_erc20_leg_verified!==true){
          setError('Swap still waiting for finalized on-chain settlement');return;
        }
        if(side==='BUY'){
          const received=exactEthUnits(body.destination_received_amount);
          setReview({from:wallet.address,to:WETH9,
            value:'0x0',amountAtomic:received,
            expiresAt:Date.now()+120000});
          attempted.current=false;
          setStage('unwrap-ready');
        }else{onTradePendingChange?.(false);
          setStage('swap-confirmed');setReview(null);}
      }
    }catch(e){if(id===serial.current)setError(e?.message||'Receipt unavailable');}
    finally{if(id===serial.current)setBusy(false);}
  }

  return <div className="mininex-swap-quote" aria-label="ETH Privy real swap">
    <strong>{t.liveSwapTitle}</strong>
    <p className="privy-wallet-description">{t.liveSwapNote}</p>
    {!attempted.current&&['idle','failed','expired','approval-confirmed','wrap-confirmed'].includes(stage)&&
      <button type="button" className="privy-wallet-action"
        disabled={!enabled||busy||!wallet||!amount} onClick={prepare}>
        {busy?'…':t.liveSwapPrepare}</button>}
    {stage==='wrap-required'&&<button type="button" className="privy-wallet-action"
      disabled={busy||Date.now()>=review?.expiresAt} onClick={()=>submit('wrap')}>
      {t.liveSwapWrap}</button>}
    {stage==='unwrap-ready'&&<button type="button" className="privy-wallet-action"
      disabled={busy||Date.now()>=review?.expiresAt} onClick={()=>submit('unwrap')}>
      {t.liveSwapUnwrap}</button>}
    {stage==='approval'&&<button type="button" className="privy-wallet-action"
      disabled={busy||Date.now()>=review?.expiresAt} onClick={()=>submit('approval')}>
      {t.liveSwapApprove}</button>}
    {stage==='ready'&&<button type="button" className="privy-wallet-action"
      disabled={busy||Date.now()>=review?.expiresAt} onClick={()=>submit('swap')}>
      {t.liveSwapSign}</button>}
    {hash&&<div><code>{hash}</code>
      <a target="_blank" rel="noopener noreferrer"
        href={'https://etherscan.io/tx/'+hash}>{t.liveSwapExplorer}</a></div>}
    {['approval-submitted','swap-submitted','wrap-submitted','unwrap-submitted'].includes(stage)&&
      <button type="button" className="privy-wallet-action"
        disabled={busy} onClick={receipt}>{t.liveSwapCheckReceipt}</button>}
    {stage==='approval-confirmed'&&<p>{t.liveSwapApprovalConfirmed}</p>}
    {stage==='wrap-confirmed'&&<p>{t.liveSwapWrapConfirmed}</p>}
    {stage==='native-complete'&&<p>{t.liveSwapNativeComplete}</p>}
    {stage==='swap-confirmed'&&<p>{t.liveSwapConfirmed}</p>}
    {error&&<p role="alert">{error}</p>}
    {busy&&<p role="status">{t.liveSwapWaiting}</p>}
  </div>;
}
