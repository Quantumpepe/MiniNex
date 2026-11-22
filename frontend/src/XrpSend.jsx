import React,{useState} from 'react';
import {Send} from 'lucide-react';
import {API_BASE} from './data.js';

// XRP send via backend (Privy signs, backend submits).
export default function XrpSend({t,enabled,userId,address,getAccessToken}){
  const [open,setOpen]=useState(false);
  const [destination,setDestination]=useState('');
  const [amount,setAmount]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [txHash,setTxHash]=useState('');

  async function send(){
    if(!enabled||!address||!destination||!amount||busy)return;
    setBusy(true);setError('');setTxHash('');
    try{
      const token=await getAccessToken?.();
      if(!token)throw new Error('Authentication required');
      const response=await fetch(API_BASE+'/api/wallet/send/xrp',{
        method:'POST',
        headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
        body:JSON.stringify({from:address,to:destination,amount:Number(amount)})
      });
      if(!response.ok){
        const err=await response.json().catch(()=>({}));
        throw new Error(err.detail||'Send failed');
      }
      const data=await response.json();
      setTxHash(data.txid||data.hash||'sent');
    }catch(e){
      setError(e.message||'Send failed');
    }finally{
      setBusy(false);
    }
  }

  if(!address)return <section className="mininex-wallet-send">
    <button type="button" className="privy-wallet-action" disabled>
      <Send size={16}/> {t.xrpWalletCreating||'XRP wallet being created…'}
    </button>
  </section>;

  return <section className="mininex-wallet-send">
    <button type="button" className="privy-wallet-action mininex-swap-open"
      onClick={()=>setOpen(v=>!v)} aria-expanded={open}>
      <Send size={18} aria-hidden="true"/>{t.walletSendTitle} XRP
    </button>
    {open&&<div className="mininex-swap-content">
      <p className="privy-wallet-description">XRP Ledger · manual transfers from your XRP wallet</p>
      <form className="mininex-send-form" onSubmit={e=>{e.preventDefault();send();}}>
        <label><span>{t.walletSendRecipient}</span>
          <input value={destination} autoComplete="off" spellCheck={false}
            placeholder="r..." disabled={busy}
            onChange={e=>setDestination(e.target.value)}/>
        </label>
        <label><span>{t.walletSendAmount} · XRP</span>
          <input value={amount} autoComplete="off" inputMode="decimal"
            placeholder="0.00" disabled={busy}
            onChange={e=>setAmount(e.target.value)}/>
        </label>
        <button type="submit" className="privy-wallet-action" disabled={busy||!destination||!amount}>
          {busy?(t.walletSendReviewing||'Sending…'):(t.walletSendTitle||'Send')}
        </button>
      </form>
      {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
      {txHash&&<p className="privy-wallet-description">{t.walletSendSubmitted||'Sent'}: {txHash.slice(0,16)}…</p>}
    </div>}
  </section>;
}
