import React,{useState} from 'react';
import {Send} from 'lucide-react';
import {useSignRawHash} from '@privy-io/react-auth/extended-chains';
import {API_BASE} from './data.js';
import SendCompletion from './SendCompletion.jsx';

// The user's existing embedded TRON wallet signs each manual TRX transfer.
// Backend only prepares/broadcasts owner-bound terms; it never signs as user.
export default function TrxSend({t,enabled,userId,address,getAccessToken}){
  const {signRawHash}=useSignRawHash();
  const [open,setOpen]=useState(false);
  const [destination,setDestination]=useState('');
  const [amount,setAmount]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [txHash,setTxHash]=useState('');

  async function request(path,body,token){
    const response=await fetch(API_BASE+path,{
      method:'POST',cache:'no-store',
      headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
      body:JSON.stringify(body),
    });
    const data=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(typeof data.detail==='string'
      ?data.detail:'TRX request failed');
    return data;
  }

  async function send(){
    if(!enabled||!userId||!address||!destination||!amount||busy)return;
    setBusy(true);setError('');setTxHash('');
    try{
      const to=destination.trim();
      const token=await getAccessToken?.();
      if(!token)throw new Error('Authentication required');
      const prepared=await request('/api/wallet/send/trx/prepare',{
        from:address,to,amount:amount.trim(),
      },token);
      if(prepared?.status!=='awaiting_wallet_signature'
          ||prepared.source!==address||prepared.destination!==to
          ||!Number.isFinite(Number(amount))
          ||Number(prepared.amount)!==Number(amount)
          ||typeof prepared.authorization!=='string'
          ||!/^0x[a-fA-F0-9]{64}$/.test(prepared.hash||''))
        throw new Error('TRX payment terms changed. Nothing was sent.');

      // Exactly one user-initiated Privy signing prompt; no silent grants.
      const signed=await signRawHash({address,chainType:'tron',hash:prepared.hash});
      if(!/^0x[a-fA-F0-9]{128}$/.test(signed?.signature||''))
        throw new Error('No valid TRX wallet signature was received');
      const freshToken=await getAccessToken?.();
      if(!freshToken)throw new Error('Authentication expired; no TRX sent');
      const submitted=await request('/api/wallet/send/trx/submit',{
        authorization:prepared.authorization,signature:signed.signature,
      },freshToken);
      if(submitted?.status!=='submitted_pending_validation'
          ||!/[a-fA-F0-9]{64}/.test(submitted?.txid||'')
          ||submitted.txid.length!==64)
        throw new Error('TRX submission status unclear; check wallet before retrying');
      setTxHash(submitted.txid);
    }catch(e){
      setError(e?.message||'TRX sending could not be completed');
    }finally{setBusy(false);}
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
      {txHash?<SendCompletion asset="TRX" txid={txHash} userId={userId}
         getAccessToken={getAccessToken} t={t}/>:<>
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
          <button type="submit" className="privy-wallet-action"
            disabled={busy||!destination||!amount}>
            {busy?(t.walletSendReviewing||'Sending…'):(t.walletSendTitle||'Send')}
          </button>
        </form>
        {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
      </>}
    </div>}
  </section>;
}
