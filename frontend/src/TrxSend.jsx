import React,{useState} from 'react';
import {Send} from 'lucide-react';

// TRX send following BTC/SOL pattern.
export default function TrxSend({t,enabled,userId,address}){
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
      // TRX send logic will be added when wallet signing is available
      setError(t.trxSendNotReady||'TRX send is being prepared. Please try again later.');
    }catch(e){
      setError(e.message||'Send failed');
    }finally{
      setBusy(false);
    }
  }

  if(!address)return <section className="mininex-wallet-send">
    <button type="button" className="privy-wallet-action" disabled>
      <Send size={16}/> {t.trxWalletCreating||'TRX wallet being created…'}
    </button>
  </section>;

  return <section className="mininex-wallet-send">
    <button type="button" className="privy-wallet-action mininex-swap-open"
      onClick={()=>setOpen(v=>!v)} aria-expanded={open}>
      <Send size={18} aria-hidden="true"/>{t.walletSendTitle} TRX
    </button>
    {open&&<div className="mininex-swap-content">
      <p className="privy-wallet-description">TRON · manual transfers from your TRX wallet</p>
      <form className="mininex-send-form" onSubmit={e=>{e.preventDefault();send();}}>
        <label><span>{t.walletSendRecipient}</span>
          <input value={destination} autoComplete="off" spellCheck={false}
            placeholder="T..." disabled={busy}
            onChange={e=>setDestination(e.target.value)}/>
        </label>
        <label><span>{t.walletSendAmount} · TRX</span>
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
