import React,{useState} from 'react';
import {useSignAndSendTransaction,useWallets} from '@privy-io/react-auth/solana';
import {Send} from 'lucide-react';
import {address,createSolanaRpc,pipe,createTransactionMessage,setTransactionMessageFeePayer,setTransactionMessageLifetimeUsingBlockhash,appendTransactionMessageInstructions,compileTransaction,createNoopSigner,getTransactionEncoder} from '@solana/kit';
import {getTransferSolInstruction} from '@solana-program/system';
import SendCompletion,{solanaSignatureBase58} from './SendCompletion.jsx';

export function exactSolLamports(raw) {
  if(typeof raw!=='string'||!/^\d+(?:\.\d{1,9})?$/.test(raw)||raw.length>32)return null;
  const [whole,frac='']=raw.split('.');
  const result=BigInt(whole)*1000000000n+BigInt(frac.padEnd(9,'0')||'0');
  return result>0n && result<=18446744073709551615n?result:null;
}

export function ownedSolanaWallet(wallets,expectedAddress) {
  if(!Array.isArray(wallets)||!expectedAddress)return null;
  const matches=wallets.filter(w=>w?.walletClientType==='privy'&&w?.address===expectedAddress);
  return matches.length===1?matches[0]:null;
}

export default function SolanaSend({t,enabled,userId,expectedAddress,getAccessToken}) {
  const {wallets}=useWallets();
  const {signAndSendTransaction}=useSignAndSendTransaction();
  const [open,setOpen]=useState(false);
  const [destination,setDestination]=useState('');
  const [amount,setAmount]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [txSig,setTxSig]=useState('');

  const wallet=ownedSolanaWallet(wallets,expectedAddress);

  async function send() {
    if(!enabled||!wallet||!destination||!amount||busy)return;
    setBusy(true);setError('');setTxSig('');
    try{
      const lamports=exactSolLamports(amount);
      if(lamports===null)throw new Error('Enter a valid positive SOL amount (up to 9 decimals)');
      if(destination===wallet.address)throw new Error('Recipient must be different from your SOL wallet');
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
      const signature=result?.signature;
      const sig=solanaSignatureBase58(signature);
      if(!sig)throw new Error('SOL submission outcome not confirmed; check wallet history before retrying');
      setTxSig(sig);
    }catch(err){
      setError(err?.message||'Send failed');
    }finally{setBusy(false);}
  }

  if(!wallet)return <section className="mininex-wallet-send"><button type="button" className="privy-wallet-action" disabled><Send size={16}/> {t.solWalletCreating||'SOL-Wallet wird erstellt…'}</button></section>;
  return <section className="mininex-wallet-send">
    <button type="button" className="privy-wallet-action" onClick={()=>setOpen(v=>!v)}>
      <Send size={16}/> {t.walletSendTitle||'Senden'} SOL
    </button>
    {open&&<div className="mininex-swap-content">
      {txSig?<SendCompletion asset="SOL" txid={txSig} userId={userId} getAccessToken={getAccessToken} t={t}/>:<>
      <p className="privy-wallet-description">Solana Mainnet · manual transfers from your SOL wallet</p>
      <form className="mininex-send-form" onSubmit={e=>{e.preventDefault();send();}}>
        <label><span>{t.walletSendRecipient}</span>
          <input value={destination} autoComplete="off" spellCheck={false}
            placeholder="Solana address..." disabled={busy}
            onChange={e=>setDestination(e.target.value)}/>
        </label>
        <label><span>{t.walletSendAmount} · SOL</span>
          <input value={amount} autoComplete="off" inputMode="decimal"
            placeholder="0.00" disabled={busy}
            onChange={e=>setAmount(e.target.value)}/>
        </label>
        <button type="submit" className="privy-wallet-action" disabled={busy||!destination||!amount}>
          {busy?(t.walletSendReviewing||'Sending…'):(t.walletSendTitle||'Send')}
        </button>
      </form>
      {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
      </>}
    </div>}
  </section>;
}
