import React,{useState} from 'react';
import {useSignRawHash} from '@privy-io/react-auth/extended-chains';
import {Send} from 'lucide-react';
import {API_BASE} from './data.js';

// Only the signed-in Privy user may approve manual XRP payments.
// The backend prepares immutable short-lived payment terms and verifies
// the owner signature before submitting to the XRP Ledger.
export default function XrpSend({t,enabled,userId,address,getAccessToken}){
  const {signRawHash}=useSignRawHash();
  const [open,setOpen]=useState(false);
  const [destination,setDestination]=useState('');
  const [amount,setAmount]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [receipt,setReceipt]=useState(null);

  async function request(path,body,token){
    const response=await fetch(API_BASE+path,{
      method:'POST',cache:'no-store',
      headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
      body:JSON.stringify(body),
    });
    const json=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(
      typeof json.detail==='string'?json.detail:'XRP transfer request failed');
    return json;
  }

  async function send(){
    if(!enabled||!userId||!address||!destination||!amount||busy)return;
    setBusy(true);setError('');setReceipt(null);
    try{
      const token=await getAccessToken?.();
      if(!token)throw new Error('Authentication required');
      const prepared=await request('/api/wallet/send/xrp/prepare',{
        from:address,to:destination.trim(),amount:amount.trim(),
      },token);
      if(prepared.source!==address||prepared.destination!==destination.trim()
        ||!Number.isFinite(Number(amount))||Number(prepared.amount)!==Number(amount)
        ||typeof prepared.hash!=='string'
        ||!/^0x[0-9a-fA-F]{64}$/.test(prepared.hash)
        ||typeof prepared.authorization!=='string')
        throw new Error('XRP payment terms changed. Nothing was sent.');
      // Privy asks the logged-in wallet owner to approve this one hash.
      const signed=await signRawHash({
        address,chainType:'xrpl',hash:prepared.hash,
      });
      if(!signed?.signature||!/^0x[0-9a-fA-F]{128}$/.test(signed.signature))
        throw new Error('No valid XRP wallet signature was received');
      const freshToken=await getAccessToken?.();
      if(!freshToken)throw new Error('Authentication expired; no XRP sent');
      const submitted=await request('/api/wallet/send/xrp/submit',{
        authorization:prepared.authorization,signature:signed.signature,
      },freshToken);
      if(!submitted?.txid||submitted.status!=='submitted_pending_validation')
        throw new Error('XRP submission status unknown; check wallet before retrying');
      setReceipt({txid:submitted.txid,status:submitted.status});
    }catch(e){
      setError(e?.message||'XRP send was not completed');
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
      <p className="privy-wallet-description">XRP Ledger · manual transfer · wallet approval required</p>
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
          {busy?(t.walletSendReviewing||'Signing…'):(t.walletSendTitle||'Send')}
        </button>
      </form>
      {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
      {receipt&&<p role="status" className="privy-wallet-description">
        {('XRP transaction submitted; awaiting ledger confirmation. Hash: ')+receipt.txid.slice(0,16)+'…'}
      </p>}
    </div>}
  </section>;
}
