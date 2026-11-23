import React,{useEffect,useRef,useState} from 'react';
import {useWallets} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {EVM_CHAINS} from './evmSwapRules.js';
import {notifyConfirmedWalletTransfer} from './walletBalanceRefresh.js';
import {validateOwnerSwapPreparation,exactNativeOwnerUnits,
  NATIVE_OWNER_SWAP_ASSETS,OWNER_SWAP_CHAINS,
  verifyOwnerEvmMinedTx} from './evmOwnerSwapPreparation.js';

// Owner-signed live V3 swaps for one EVM coin and its selected stablecoin.
// MiniNex's authenticated backend produces a fresh on-chain quote and exact
// nonzero minimum output. No V2 builder, client-derived router or zero-slippage
// transaction may ever be broadcast from this component.
export default function EvmLiveSwap({
  enabled,userId,asset,side,stable,amount,getAccessToken,lang='de',t,
}){
  const {wallets}=useWallets();
  const wallet=wallets.find(w=>w?.walletClientType==='privy'&&
    /^0x[0-9a-fA-F]{40}$/.test(w?.address||''));
  const native=NATIVE_OWNER_SWAP_ASSETS.has(asset);
  const chain=OWNER_SWAP_CHAINS[asset];
  const config=EVM_CHAINS[native?asset:asset==='ARB'?'ARB':'ETH'];
  const [plan,setPlan]=useState(null);
  const [phase,setPhase]=useState('idle');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [pending,setPending]=useState(null);
  const [unwrapAmount,setUnwrapAmount]=useState(null);
  const serial=useRef(0);
  const attempt=useRef(false);
  const reportedReceipt=useRef('');
  const words={
    de:{prepare:'Swap vorbereiten',approve:'Token-Freigabe bestätigen',
      wrap:'Coin für Swap vorbereiten',swap:'Swap mit Privy bestätigen',
      unwrap:'Coin-Auszahlung bestätigen',pending:'Wird bestätigt …',
      retry:'Erneut vorbereiten',insufficient:'Guthaben für Swap nicht ausreichend',
      gas:'Netzwerkguthaben für Gebühren fehlt',changed:'Transaktion verändert',
      expired:'Angebot abgelaufen – neu vorbereiten',
      uncertain:'Übermittlung unklar. Wallet prüfen, nicht erneut senden.',
      wrapped:'Swap bestätigt. Auszahlung in den nativen Coin freigeben.',
      ready:'Angebot geprüft. Bestätige den Swap mit Privy.',
      approvalDone:'Freigabe bestätigt. Swap erneut vorbereiten.',
      wrapDone:'Coin vorbereitet. Swap erneut vorbereiten.'},
    en:{prepare:'Prepare swap',approve:'Approve token amount',
      wrap:'Prepare native coin for swap',swap:'Confirm swap in Privy',
      unwrap:'Confirm native coin withdrawal',pending:'Confirming…',
      retry:'Prepare again',insufficient:'Insufficient balance for this swap',
      gas:'Native network gas balance unavailable',changed:'Transaction changed',
      expired:'Quote expired – prepare again',
      uncertain:'Submission uncertain. Check your wallet before sending again.',
      wrapped:'Swap confirmed. Approve native coin withdrawal.',
      ready:'Quote verified. Confirm swap in your Privy wallet.',
      approvalDone:'Approval confirmed. Prepare the swap again.',
      wrapDone:'Native coin prepared. Prepare the swap again.'},
  }[lang==='de'?'de':'en'];
  const done=t?.walletSendDone||(lang==='de'?'Erledigt':'Done');
  const getToken=async()=>{
    const token=await getAccessToken?.();
    if(!token)throw Error('Privy login expired');
    return token;
  };
  async function backend(url,method='GET',body){
    const token=await getToken();
    const response=await fetch(API_BASE+url,{
      method,cache:'no-store',
      headers:{Authorization:'Bearer '+token,Accept:'application/json',
        ...(method==='POST'?{'Content-Type':'application/json'}:{})},
      ...(body?{body:JSON.stringify(body)}:{}),
    });
    if(!response.ok)throw Error('Verified on-chain swap quote unavailable');
    return response.json();
  }
  async function provider(){
    if(!wallet||!chain||!enabled||typeof wallet.getEthereumProvider!=='function')
      throw Error('Privy EVM wallet unavailable');
    let p=await wallet.getEthereumProvider();
    if(!p||typeof p.request!=='function')throw Error('Wallet provider unavailable');
    if(BigInt(await p.request({method:'eth_chainId'}))!==BigInt(chain)){
      if(typeof wallet.switchChain!=='function')throw Error('Network change unavailable');
      await wallet.switchChain(chain);
      p=await wallet.getEthereumProvider();
    }
    if(!p||BigInt(await p.request({method:'eth_chainId'}))!==BigInt(chain))
      throw Error('Wrong EVM network');
    const accounts=await p.request({method:'eth_accounts'});
    if(!Array.isArray(accounts)||!accounts.some(a=>
        typeof a==='string'&&a.toLowerCase()===wallet.address.toLowerCase()))
      throw Error('Privy wallet identity changed');
    return p;
  }
  async function prepare(){
    if(!enabled||!wallet||busy||attempt.current||!chain)return;
    const id=++serial.current;
    setBusy(true);setError('');setPlan(null);setPhase('preparing');
    try{
      const data=await backend('/api/live/evm/owner-swap-preparation/'+asset,
        'POST',{side,stable,amount});
      const next=validateOwnerSwapPreparation(data,{
        asset,side,stable,amount,wallet:wallet.address,
      });
      // Verified backend response must refer to the same wrapped native token
      // as the chain's wallet contract. EVM tokens have no wrap step.
      if(native&&(!config?.wrapped||
          ![next.source,next.output].some(a=>a.toLowerCase()===config.wrapped.toLowerCase())))
        throw Error('Native wrapped token route mismatch');
      if(id!==serial.current)return;
      setPlan(next);
      if(!next.sourceAvailable){
        if(next.wrapPossible)setPhase('wrap-ready');
        else {setPhase('failed');setError(words.insufficient);}
      }else if(!next.gasAvailable){
        setPhase('failed');setError(words.gas);
      }else if(next.approve){
        setPhase('approve-ready');
      }else if(next.swap){
        setPhase('swap-ready');
      }else{
        setPhase('failed');setError('No owner-authorized transaction available');
      }
    }catch(e){
      if(id===serial.current){setError(e?.message||'Swap unavailable');setPhase('failed');}
    }finally{if(id===serial.current)setBusy(false);}
  }
  async function nativeWrapAmount(p){
    const required=exactNativeOwnerUnits(amount);
    const wrapped=config?.wrapped;
    if(!wrapped)throw Error('Wrapped native token unavailable');
    const balance=await p.request({method:'eth_call',params:[{
      to:wrapped,data:'0x70a08231'+wallet.address.slice(2).toLowerCase().padStart(64,'0'),
    },'latest']});
    if(typeof balance!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(balance))
      throw Error('Wrapped balance unavailable');
    const missing=required-BigInt(balance);
    if(missing<=0n)throw Error('Wrapped funds already available; prepare again');
    const nativeBalance=BigInt(await p.request({method:'eth_getBalance',
      params:[wallet.address,'latest']}));
    const tx={from:wallet.address,to:wrapped,data:'0xd0e30db0',
      value:'0x'+missing.toString(16)};
    const estimate=BigInt(await p.request({method:'eth_estimateGas',params:[tx]}));
    const price=BigInt(await p.request({method:'eth_gasPrice'}));
    if(nativeBalance<=missing+estimate*price)
      throw Error('Insufficient native balance including network fee');
    return tx;
  }
  async function send(kind){
    if(!enabled||busy||attempt.current||!wallet||!plan)return;
    const allowed={approval:'approve-ready',wrap:'wrap-ready',swap:'swap-ready',
      unwrap:'unwrap-ready'};
    if(phase!==allowed[kind])return;
    if(kind==='swap'&&(!plan.swap||Date.now()>=plan.swap.expiresAt)){
      setPhase('failed');setError(words.expired);return;
    }
    let broadcastAttempted=false;
    setBusy(true);setError('');
    try{
      const p=await provider();
      let tx;
      if(kind==='approval'){
        if(!plan.approve)throw Error(words.changed);
        tx={from:plan.approve.from,to:plan.approve.to,
          value:'0x0',data:plan.approve.data};
      }else if(kind==='wrap'){
        tx=await nativeWrapAmount(p);
      }else if(kind==='unwrap'){
        const atomic=exactNativeOwnerUnits(unwrapAmount);
        const wrapped=config?.wrapped;
        if(!native||!wrapped||!plan.output||
            wrapped.toLowerCase()!==plan.output.toLowerCase())
          throw Error(words.changed);
        const balance=await p.request({method:'eth_call',params:[{
          to:wrapped,data:'0x70a08231'+wallet.address.slice(2).toLowerCase().padStart(64,'0'),
        },'latest']});
        if(typeof balance!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(balance)
            ||BigInt(balance)<atomic)throw Error('Wrapped coin balance unavailable');
        tx={from:wallet.address,to:wrapped,value:'0x0',
          data:'0x2e1a7d4d'+atomic.toString(16).padStart(64,'0')};
      }else{
        if(!plan.swap||Date.now()>=plan.swap.expiresAt)
          throw Error(words.expired);
        tx={from:plan.swap.from,to:plan.swap.to,
          value:'0x0',data:plan.swap.data};
      }
      const chainNow=BigInt(await p.request({method:'eth_chainId'}));
      if(chainNow!==BigInt(plan.chainId))throw Error(words.changed);
      attempt.current=true;broadcastAttempted=true;
      const hash=await p.request({method:'eth_sendTransaction',params:[tx]});
      if(typeof hash!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(hash))
        throw Error('Transaction hash unavailable');
      setPending({hash,kind,tx,plan,asset,side,stable,userId});
      setPhase('submitted');
    }catch(e){
      if(broadcastAttempted&&e?.code!==4001&&e?.code!=='ACTION_REJECTED'){
        setPhase('uncertain');setError(words.uncertain);
      }else{
        attempt.current=false;
        setPhase('failed');setError(e?.message||'Wallet signing unavailable');
      }
    }finally{setBusy(false);}
  }
  const refreshIncludedBalance=hash=>{
    const fingerprint=userId+':'+chain+':'+hash.toLowerCase();
    if(reportedReceipt.current===fingerprint)return;
    reportedReceipt.current=fingerprint;
    // Inclusion refreshes on-chain wallet balances, NOT trade finality.
    notifyConfirmedWalletTransfer(userId,asset,hash);
  };
  useEffect(()=>{
    if(phase!=='submitted'||!pending||!enabled)return;
    let active=true,abort=false;
    let timeout;
    const check=async()=>{
      if(!active||abort)return;
      try{
        const p=await provider();
        const receipt=await p.request({method:'eth_getTransactionReceipt',
          params:[pending.hash]});
        if(!active)return;
        if(!receipt){timeout=setTimeout(check,13000);return;}
        if(receipt.transactionHash?.toLowerCase()!==pending.hash.toLowerCase()
           ||receipt.from?.toLowerCase()!==wallet.address.toLowerCase()
           ||receipt.to?.toLowerCase()!==pending.tx.to.toLowerCase()){
          // An unexpected receipt is NOT evidence that the original send
          // reverted. Keep its one-shot lock until the owner reconciles it.
          setError(words.uncertain);setPhase('uncertain');return;
        }
        const original=await p.request({method:'eth_getTransactionByHash',
          params:[pending.hash]});
        if(!active)return;
        if(!original){timeout=setTimeout(check,13000);return;}
        try {
          verifyOwnerEvmMinedTx(original,pending.tx,pending.hash,pending.plan.chainId);
        }catch{
          setError(words.uncertain);setPhase('uncertain');return;
        }
        if(receipt.status==='0x0'){
          // A matching mined REVERT proves this particular send did not
          // execute. Chargeable gas may have changed the native balance;
          // offer a NEW owner-approved quote, never replay the old bytes.
          refreshIncludedBalance(pending.hash);
          attempt.current=false;
          setPending(null);setPlan(null);
          setError(lang==='de'
            ?'Transaktion auf der Blockchain fehlgeschlagen. Bitte neu vorbereiten.'
            :'Transaction reverted on-chain. Prepare a new quote.');
          setPhase('failed');return;
        }
        if(receipt.status!=='0x1'){
          setError(words.uncertain);setPhase('uncertain');return;
        }
        if(pending.kind==='swap'){
          refreshIncludedBalance(pending.hash);
          const saved=await backend('/api/live/evm/finalized-swap/'+asset+'/'+
            encodeURIComponent(pending.hash)+'?side='+side+'&stable='+stable);
          if(!active)return;
          if(saved?.state!=='FINALIZED_ERC20_SWAP_LEG'
              ||saved?.swap_erc20_leg_verified!==true){
            timeout=setTimeout(check,16000);return;
          }
          if(native&&side==='BUY'){
            // A native-coin buy yields wrapped BNB/POL/AVAX first. The
            // owner must explicitly unwrap the exact finalized amount.
            exactNativeOwnerUnits(saved.destination_received_amount);
            setUnwrapAmount(saved.destination_received_amount);
            setPhase('unwrap-ready');
            attempt.current=false;
            return;
          }
          setPhase('complete');return;
        }
        // Approvals and wrap/unwrap also spend network gas; refresh their
        // verified on-chain balances once, without implying a settled trade.
        refreshIncludedBalance(pending.hash);
        if(pending.kind==='approval'){
          setPhase('approval-complete');
        }else if(pending.kind==='wrap'){
          setPhase('wrap-complete');
        }else if(pending.kind==='unwrap'){
          setPhase('complete');
        }
      }catch{
        if(active)timeout=setTimeout(check,16000);
      }
    };
    timeout=setTimeout(check,12000);
    return()=>{active=false;abort=true;clearTimeout(timeout);};
  },[phase,pending,enabled,userId,asset,side,stable]);
  // An already broadcast transaction is never auto-retried. The user may
  // prepare a fresh quote only after an explicit approval/wrap receipt.
  const retry=()=>{
    attempt.current=false;
    setPending(null);setPlan(null);setPhase('idle');setError('');
  };
  if(!enabled||!chain||!wallet)return null;
  return <div className="mininex-swap-quote" aria-label="Privy EVM manual swap">
    {phase==='idle'&&<button type="button" className="privy-wallet-action"
      disabled={busy||!amount} onClick={prepare}>{words.prepare}</button>}
    {phase==='preparing'&&<p role="status">{words.pending}</p>}
    {phase==='swap-ready'&&<>
      <p className="privy-wallet-description">{words.ready}</p>
      <button type="button" className="privy-wallet-action" disabled={busy}
        onClick={()=>send('swap')}>{words.swap}</button>
    </>}
    {phase==='approve-ready'&&<button type="button" className="privy-wallet-action"
      disabled={busy} onClick={()=>send('approval')}>{words.approve}</button>}
    {phase==='wrap-ready'&&<button type="button" className="privy-wallet-action"
      disabled={busy} onClick={()=>send('wrap')}>{words.wrap}</button>}
    {phase==='unwrap-ready'&&<>
      <p className="privy-wallet-description">{words.wrapped}</p>
      <button type="button" className="privy-wallet-action"
        disabled={busy} onClick={()=>send('unwrap')}>{words.unwrap}</button>
    </>}
    {(phase==='submitted'||busy&&phase!=='preparing')&&
      <p role="status" className="privy-wallet-description">{words.pending}</p>}
    {phase==='complete'&&<p role="status" className="privy-wallet-description">{done}</p>}
    {['approval-complete','wrap-complete'].includes(phase)&&<>
      <p role="status" className="privy-wallet-description">
        {phase==='approval-complete'?words.approvalDone:words.wrapDone}
      </p>
      <button type="button" className="privy-wallet-action"
        onClick={retry}>{words.retry}</button>
    </>}
    {phase==='failed'&&error&&<p role="alert" className="privy-wallet-description">{error}</p>}
    {phase==='failed'&&!attempt.current&&<button type="button"
      className="privy-wallet-action" onClick={retry}>{words.retry}</button>}
    {phase==='uncertain'&&<p role="alert" className="privy-wallet-description">{words.uncertain}</p>}
  </div>;
}
