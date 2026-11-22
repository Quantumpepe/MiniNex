import React,{useState} from 'react';
import {useSignAndSendTransaction,useWallets} from '@privy-io/react-auth/solana';
import {Send} from 'lucide-react';
import {address,createSolanaRpc,pipe,createTransactionMessage,setTransactionMessageFeePayer,setTransactionMessageLifetimeUsingBlockhash,appendTransactionMessageInstructions,compileTransaction,createNoopSigner,getTransactionEncoder} from '@solana/kit';
import {getTransferSolInstruction} from '@solana-program/system';

export default function SolanaSend({t,enabled,userId}) {
  const {wallets}=useWallets();
  const {signAndSendTransaction}=useSignAndSendTransaction();
  const [open,setOpen]=useState(false);
  const [destination,setDestination]=useState('');
  const [amount,setAmount]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [txSig,setTxSig]=useState('');

  const wallet=wallets.find(w=>w?.walletClientType==='privy'||w?.chainType==='solana'||w?.address);

  async function send() {
    if(!enabled||!wallet||!destination||!amount)return;
    setBusy(true);setError('');setTxSig('');
    try{
      const lamports=BigInt(Math.round(Number(amount)*1_000_000_000));
      const rpc=createSolanaRpc('https://api.mainnet-beta.solana.com');
      const {value:latestBlockhash}=await rpc.getLatestBlockhash().send();
      const instruction=getTransferSolInstruction({
        amount:lamports,
        destination:address(destination),
        source:createNoopSigner(address(wallet.address)),
      });
      const transaction=pipe(
        createTransactionMessage({version:0}),
        tx=>setTransactionMessageFeePayer(address(wallet.address),tx),
        tx=>setTransactionMessageLifetimeUsingBlockhash(latestBlockhash,tx),
        tx=>appendTransactionMessageInstructions([instruction],tx),
        tx=>compileTransaction(tx),
        tx=>new Uint8Array(getTransactionEncoder().encode(tx)),
      );
      const result=await signAndSendTransaction({transaction,wallet});
      setTxSig(Buffer.from(result.signature).toString('base64'));
    }catch(err){
      setError(err?.message||'Send failed');
    }finally{setBusy(false);}
  }

  if(!wallet)return <section className="mininex-wallet-send"><button type="button" className="privy-wallet-action" disabled><Send size={16}/> {t.solWalletCreating||'SOL-Wallet wird erstellt…'}</button></section>;
  return <section className="mininex-wallet-send">
    <button type="button" className="privy-wallet-action" onClick={()=>setOpen(v=>!v)}>
      <Send size={16}/> {t.walletSendTitle||'Senden'} SOL
    </button>
    {open&&<div className="mininex-send-content">
      <label><span>Empfänger</span>
        <input value={destination} onChange={e=>setDestination(e.target.value)} placeholder={t.walletSendRecipient||'Solana-Adresse'}/>
      </label>
      <label><span>Betrag (SOL)</span>
        <input type="number" value={amount} onChange={e=>setAmount(e.target.value)} placeholder="0.01"/>
      </label>
      <button type="button" className="privy-wallet-action" disabled={busy||!destination||!amount} onClick={send}>
        {busy?(t.walletSendReviewing||'Senden…'):(t.walletSendTitle||'Senden')}
      </button>
      {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
      {txSig&&<p className="privy-wallet-description">Gesendet: {txSig.slice(0,16)}…</p>}
    </div>}
  </section>;
}
