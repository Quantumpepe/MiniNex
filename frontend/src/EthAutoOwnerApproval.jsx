import React,{useEffect,useRef,useState} from 'react';
import {useWallets} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {AUTO_APPROVAL_TOKENS,validateEthAutoOwnerApproval,
  zeroAllowanceCalldata,ownerAccounts,abiAllowance,decodeUint,validHash
} from './ethAutoApprovalRules.js';

// Owner-initiated token approve, not an automated Privy additional-signer grant.
export default function EthAutoOwnerApproval({
  userId,walletAddress,getAccessToken,paused,enabled,t,
}){
  const {wallets}=useWallets();
  const [token,setToken]=useState('USDC');
  const [prepared,setPrepared]=useState(null);
  const [stage,setStage]=useState('idle');
  const [hash,setHash]=useState('');
  const [message,setMessage]=useState('');
  const [busy,setBusy]=useState(false);
  const provider=useRef(null);
  const pending=useRef(false);
  const wallet=wallets.find(w=>w?.walletClientType==='privy'&&
    w?.address?.toLowerCase()===walletAddress?.toLowerCase());
  useEffect(()=>{
    setPrepared(null);setStage('idle');setHash('');
    setMessage('');setBusy(false);
    provider.current=null;pending.current=false;
  },[userId,walletAddress,token,paused]);
  const allowed=enabled&&paused&&wallet&&getAccessToken&&
    /^0x[0-9a-fA-F]{40}$/.test(walletAddress||'');
  async function getProvider(){
    const p=await wallet?.getEthereumProvider?.();
    if(!p||typeof p.request!=='function')throw Error('Privy wallet provider missing');
    if(BigInt(await p.request({method:'eth_chainId'}))!==1n)
      throw Error('Switch wallet to Ethereum mainnet');
    if(!ownerAccounts(await p.request({method:'eth_accounts'}),walletAddress))
      throw Error('Only the owner embedded wallet can approve');
    return p;
  }
  async function prepare(){
    if(!allowed||busy||pending.current)return;
    setBusy(true);setPrepared(null);setMessage('');
    try{
      const secret=await getAccessToken();
      if(!secret)throw Error('Privy login required');
      const res=await fetch(API_BASE+
        '/api/live/eth/owner-approval-plan?token='+encodeURIComponent(token),{
          cache:'no-store',headers:{Authorization:'Bearer '+secret}});
      if(!res.ok)throw Error('ETH allowance plan unavailable');
      const plan=validateEthAutoOwnerApproval(await res.json(),{
        wallet:walletAddress,token});
      const p=await getProvider();
      const allowance=decodeUint(await p.request({
        method:'eth_call',params:[{
          to:plan.token,data:abiAllowance(walletAddress,plan.spender),
        },'latest']}));
      if(allowance>=plan.amount){
        setStage('approved');setMessage(t.liveEthApprovalAlready);
        return;
      }
      setPrepared({ ...plan,resetFirst:allowance>0n });
      provider.current=p;setStage('ready');
    }catch(e){
      setStage('failed');setMessage(e?.message||t.liveEthApprovalFailed);
    }finally{setBusy(false);}
  }
  async function approve(){
    if(!allowed||busy||pending.current||stage!=='ready'||!prepared)return;
    if(Date.now()>=prepared.expires){setStage('expired');setPrepared(null);return;}
    setBusy(true);setMessage('');
    pending.current=true;
    try{
      const p=await getProvider();
      const data=prepared.resetFirst?zeroAllowanceCalldata():prepared.data;
      const tx=await p.request({method:'eth_sendTransaction',params:[{
        from:walletAddress,to:AUTO_APPROVAL_TOKENS[token],
        value:'0x0',data,
      }]});
      if(!validHash(tx))throw Error('Unknown Privy approval result');
      provider.current=p;setHash(tx);setStage('submitted');
    }catch(e){
      if(e?.code===4001||e?.code==='ACTION_REJECTED'){
        pending.current=false;setStage('idle');
        setMessage(t.liveEthApprovalCancelled);
      }else{
        setStage('uncertain');setMessage(t.liveEthApprovalUncertain);
      }
    }finally{setBusy(false);}
  }
  async function confirm(){
    if(!hash||!provider.current||busy||stage!=='submitted')return;
    setBusy(true);setMessage('');
    try{
      const p=provider.current;
      const receipt=await p.request({method:'eth_getTransactionReceipt',params:[hash]});
      if(!receipt){setMessage(t.liveEthApprovalPending);return;}
      if(receipt.transactionHash?.toLowerCase()!==hash.toLowerCase()||
         receipt.from?.toLowerCase()!==walletAddress?.toLowerCase()||
         receipt.to?.toLowerCase()!==AUTO_APPROVAL_TOKENS[token]||
         receipt.status!=='0x1')throw Error('Token approval reverted or mismatched');
      const remaining=decodeUint(await p.request({
        method:'eth_call',params:[{
          to:AUTO_APPROVAL_TOKENS[token],
          data:abiAllowance(walletAddress,prepared.spender),
        },'latest']}));
      if(prepared.resetFirst){
        if(remaining!==0n)throw Error('Zero-reset not reflected on chain');
        setStage('reset-done');
        setMessage(t.liveEthApprovalResetDone);
      }else if(remaining>=prepared.amount){
        setStage('approved');setMessage(t.liveEthApprovalConfirmed);
      }else throw Error('On-chain allowance not yet updated');
      pending.current=false;setPrepared(null);
    }catch(e){setMessage(e?.message||t.liveEthApprovalFailed);}
    finally{setBusy(false);}
  }
  const waiting=stage==='submitted'||stage==='uncertain';
  return <div className="privy-hwm-box" style={{display:'grid',gap:8}}>
    <strong>{t.liveEthApprovalTitle}</strong>
    <p className="privy-wallet-description">{t.liveEthApprovalDescription}</p>
    <select aria-label={t.liveEthApprovalToken} value={token}
      disabled={busy||waiting} onChange={e=>setToken(e.target.value)}>
      <option value="USDC">USDC</option>
      <option value="USDT">USDT</option>
      <option value="WETH">WETH</option>
    </select>
    {prepared&&<p className="privy-wallet-description">
      {t.liveEthApprovalCap}: {prepared.approvalCapUsd} USD
      {' · '}{t.liveEthApprovalQuantity}: {prepared.amount.toString()}
      {' · '}{token}
    </p>}
    {['idle','failed','expired','reset-done','approved'].includes(stage)&&
      <button type="button" className="privy-wallet-action"
        disabled={!allowed||busy} onClick={prepare}>
        {t.liveEthApprovalPrepare}</button>}
    {stage==='ready'&&<button type="button" className="privy-wallet-action"
      disabled={!allowed||busy} onClick={approve}>
      {prepared?.resetFirst?t.liveEthApprovalReset:t.liveEthApprovalSign}</button>}
    {stage==='submitted'&&<button type="button" className="privy-wallet-action"
      disabled={busy} onClick={confirm}>{t.liveEthApprovalReceipt}</button>}
    {hash&&<a href={'https://etherscan.io/tx/'+hash}
      target="_blank" rel="noopener noreferrer">{t.liveEthApprovalExplorer}</a>}
    {message&&<p role="status" className="privy-wallet-description">{message}</p>}
    {!paused&&<p className="privy-wallet-description">
      {t.liveEthApprovalPauseFirst}</p>}
  </div>;
}
