import React,{useEffect,useRef,useState} from 'react';
import {Send} from 'lucide-react';
import {Buffer} from 'buffer';
import {useSignRawHash} from '@privy-io/react-auth/extended-chains';
import {btcSatoshis,btcOwnerScript,btcOwnedPublicKey,btcRecipientScript,
  btcFeeRate,btcValidatedUtxos,btcPlan,btcMaxPlan,btcDisplay,
  btcFetch,btcSignAndBroadcast,btcRecoverPublicKey,BTC_REVIEW_MAX_AGE_MS} from './btcSendRules.js';

// This is the native BTC wallet, not an EVM/wrapped-BTC route. The user
// explicitly reviews every Bitcoin mainnet spend; no delegation or auto-send.
export default function BtcWalletSend({t,lang='de',userId,wallet,enabled}) {
  const {signRawHash}=useSignRawHash();
  const [open,setOpen]=useState(false);
  const [destination,setDestination]=useState('');
  const [amount,setAmount]=useState('');
  const [review,setReview]=useState(null);
  const [stage,setStage]=useState('idle');
  const [txid,setTxid]=useState(null);
  const [lastSignedTxid,setLastSignedTxid]=useState(null);
  const [issue,setIssue]=useState('');
  const pending=useRef(false);
  const request=useRef(0);
  const controller=useRef(null);
  const [nonce,setNonce]=useState(0);
  const [verifiedPubkey,setVerifiedPubkey]=useState(null);
  const [verifying,setVerifying]=useState(false);
  const de=lang==='de';
  const address=wallet?.address||null;
  // Store only a public BTC transaction hash, never signed wire, keys or
  // Privy credentials. This permits recovery of its explorer link after an
  // OTA reload or ambiguous network acknowledgement on the same account.
  const signedKey=userId&&/^bc1q[ac-hj-np-z02-9]{38}$/.test(address||'')?
    'mininex:last-signed-btc:'+userId+':'+address:null;
  const rememberSignedTxid=hash=>{
    if(!signedKey||!/^[a-f0-9]{64}$/.test(hash))return;
    setLastSignedTxid(hash);
    try{globalThis.localStorage?.setItem(signedKey,hash);}catch{/* private mode */}
  };
  const publicKey=btcOwnedPublicKey(wallet)||verifiedPubkey;
  const ownerScript=btcOwnerScript(address,publicKey);
  const supported=enabled&&ownerScript&&typeof signRawHash==='function';

  useEffect(()=>{
    request.current++;
    controller.current?.abort();
    controller.current=null;
    pending.current=false;
    setOpen(false);setDestination('');setAmount('');setReview(null);
    setStage('idle');setTxid(null);setIssue('');
    setVerifiedPubkey(null);setVerifying(false);
    let saved=null;
    if(signedKey){
      try{saved=globalThis.localStorage?.getItem(signedKey);}catch{/* private mode */}
    }
    setLastSignedTxid(/^[a-f0-9]{64}$/.test(saved||'')?saved:null);
  },[userId,address,enabled,nonce,signedKey]);

  const verifyWallet=async()=>{
    if(!enabled||verifying||!address||!/^bc1q[ac-hj-np-z02-9]{38}$/.test(address)||
       typeof signRawHash!=='function')return;
    setVerifying(true);setIssue('');
    try {
      if(!globalThis.crypto?.getRandomValues)throw Error('Secure random source unavailable');
      const challenge=globalThis.crypto.getRandomValues(new Uint8Array(32));
      const signed=await signRawHash({address,chainType:'bitcoin-segwit',
        hash:'0x'+Buffer.from(challenge).toString('hex')});
      const key=btcRecoverPublicKey(address,challenge,signed?.signature);
      if(!key)throw Error('Wallet public key did not match the linked address');
      setVerifiedPubkey(key);
    }catch{
      setIssue(de?'Die Privy-BTC-Signatur konnte nicht bestätigt werden. Nichts versendet.':
        'Could not verify Privy BTC signature. Nothing was sent.');
    }finally{setVerifying(false);}
  };
  const clearReview=()=>{
    request.current++;
    controller.current?.abort();
    controller.current=null;
    setReview(null);setIssue('');setStage('idle');
  };
  const check=async(event,useMax=false)=>{
    event.preventDefault();
    if(!supported||pending.current||
       (!useMax&&!btcSatoshis(amount))||!btcRecipientScript(destination))return;
    clearReview();
    const id=++request.current;
    const ctrl=new AbortController();
    controller.current=ctrl;
    pending.current=true;setStage('checking');
    try{
      const [coins,fees]=await Promise.all([
        btcFetch('/address/'+encodeURIComponent(address)+'/utxo',{signal:ctrl.signal}),
        btcFetch('/v1/fees/recommended',{signal:ctrl.signal})
      ]);
      const utxos=btcValidatedUtxos(coins),rate=btcFeeRate(fees);
      const plan=useMax?
        btcMaxPlan({destination,utxos,feeRate:rate,sourceScript:ownerScript}):
        btcPlan({destination,amount,utxos,feeRate:rate,sourceScript:ownerScript});
      if(!plan)throw Error('Not enough confirmed BTC or fee quote unavailable');
      if(id===request.current&&!ctrl.signal.aborted){
        if(useMax)setAmount(plan.amount);
        setReview(plan);setStage('review');
      }
    }catch{
      if(id===request.current&&!ctrl.signal.aborted){
        setIssue(de?'BTC-Guthaben oder Netzwerkgebühr nicht prüfbar. Nichts versendet.':
          'Could not verify BTC funds or network fee. Nothing sent.');
        setStage('failed');
      }
    }finally{pending.current=false;}
  };
  const send=async()=>{
    if(!supported||!review||pending.current||stage!=='review')return;
    if(Date.now()-review.createdAt>BTC_REVIEW_MAX_AGE_MS){
      setReview(null);setStage('idle');
      setIssue(t.walletSendReviewExpired);return;
    }
    pending.current=true;setStage('sending');setIssue('');
    const id=++request.current;
    const ctrl=new AbortController();
    controller.current=ctrl;
    let signedTxid=null;
    try{
      // Re-check the current confirmed UTXO set after review, before signing.
      const fresh=btcValidatedUtxos(await btcFetch(
        '/address/'+encodeURIComponent(address)+'/utxo',{signal:ctrl.signal}));
      const reviewed=review.utxos.map(u=>u.txid+':'+u.vout+':'+u.value);
      const current=new Set(fresh?.map(u=>u.txid+':'+u.vout+':'+u.value)||[]);
      if(!fresh||reviewed.some(key=>!current.has(key)))
        throw Error('Bitcoin funds changed; review again');
      const hash=await btcSignAndBroadcast(review,{address,pubkey:publicKey,
        signRawHash,signal:ctrl.signal,onSigned:hash=>{
          signedTxid=hash;
          if(id===request.current){
            setTxid(hash);
            rememberSignedTxid(hash);
          }
        }});
      if(id===request.current){setTxid(hash);setReview(null);setStage('submitted');}
    }catch{
      if(id===request.current){
        setStage(signedTxid?'uncertain':'failed');
        setReview(null);
        setIssue(signedTxid?(
          de?'BTC-Sendeausgang unklar. Prüfe zuerst die signierte Transaktions-ID im Explorer. Nicht erneut senden.':
             'BTC submission uncertain. Check the signed transaction ID in the explorer. Do not resend.'
        ):(
          de?'BTC-Prüfung oder Signierung nicht erfolgreich. Es wurde nichts versendet.':
             'BTC verification or signing failed before broadcast. Nothing was sent.'
        ));
      }
    }finally{pending.current=false;}
  };
  return <section className="mininex-wallet-send mininex-btc-send">
    <button type="button" className="privy-wallet-action mininex-swap-open"
      onClick={()=>{if(pending.current)return;clearReview();setOpen(v=>!v);}}
      aria-expanded={open} aria-controls="mininex-btc-send-form">
      <Send size={18} aria-hidden="true"/>{t.walletSendTitle} BTC
    </button>
    {open&&<div className="mininex-swap-content" id="mininex-btc-send-form">
      <p className="privy-wallet-description">
        {de?'Bitcoin Mainnet · nur manuell aus deiner BTC-Wallet':
          'Bitcoin Mainnet · manual transfers from your BTC wallet'}
      </p>
      {!supported&&enabled&&address&&typeof signRawHash==='function'&&
        <button type="button" className="privy-wallet-action"
          onClick={verifyWallet} disabled={verifying}>
          {verifying?(de?'BTC-Wallet wird geprüft…':'Verifying BTC wallet…'):
            (de?'BTC-Wallet mit Privy prüfen':'Verify BTC wallet with Privy')}
        </button>}
      {!supported&&<p className="privy-wallet-description" role="status">
        {de?'BTC-Signierung dieser Privy-Wallet ist noch nicht verifiziert. Kein Versand möglich.':
          'BTC signing for this Privy wallet is not verified yet. Sending unavailable.'}
      </p>}
      <form className="mininex-send-form" onSubmit={check}>
        <label><span>{t.walletSendRecipient}</span>
          <input value={destination} autoComplete="off" spellCheck={false}
            placeholder="bc1..." disabled={!supported||stage==='sending'}
            onChange={event=>{clearReview();setDestination(event.target.value.trim());}}/>
        </label>
        <label><span>{t.walletSendAmount} · BTC</span>
          <input value={amount} autoComplete="off" inputMode="decimal"
            placeholder="0.00000000" disabled={!supported||stage==='sending'}
            onChange={event=>{clearReview();setAmount(event.target.value);}}/>
          <button type="button" className="privy-wallet-action"
            disabled={!supported||stage==='checking'||stage==='sending'||
              !btcRecipientScript(destination)}
            onClick={event=>check(event,true)}>
            {t.walletSendMax||'Max'} BTC
          </button>
        </label>
        <button type="submit" className="privy-wallet-action"
          disabled={!supported||stage==='checking'||stage==='sending'||
            !btcSatoshis(amount)||!btcRecipientScript(destination)}>
          {stage==='checking'?t.walletSendReviewing:t.walletSendReview}
        </button>
      </form>
      {issue&&<p role="alert" className="privy-wallet-description">{issue}</p>}
      {stage==='review'&&review&&<div className="mininex-swap-quote" role="group"
        aria-label={t.walletSendSummary}>
        <strong>{t.walletSendSummary} · Bitcoin Mainnet</strong>
        <div>{t.walletSendRecipient}: <code>{review.destination}</code></div>
        <div>{t.walletSendAmount}: {btcDisplay(review.sats)} BTC</div>
        <div>{t.walletSendFeeMax}: {btcDisplay(review.fee)} BTC
          {' ('+review.feeRate+' sat/vB)'}</div>
        <div>{de?'Rückgeld an eigene Wallet':'Change to own wallet'}: {btcDisplay(review.change)} BTC</div>
        <p className="privy-wallet-description">{de?
          'Nach deiner Bestätigung wird die BTC-Transaktion signiert und gesendet. Unwiderruflich.':
          'After confirmation the BTC transaction will be signed and broadcast. Irreversible.'}</p>
        <button type="button" className="privy-wallet-action"
          onClick={send}>{t.walletSendConfirm}</button>
      </div>}
      {stage==='sending'&&<p role="status" className="privy-wallet-description">
        {de?'Privy-Signierung und Bitcoin-Versand laufen …':
          'Privy signing and Bitcoin broadcast in progress…'}
      </p>}
      {stage==='uncertain'&&txid&&<p role="status" className="privy-wallet-description">
        {de?'Signierte Transaktions-ID (Versand nicht bestätigt)':
          'Signed transaction ID (broadcast not confirmed)'}:
        {' '}<a href={'https://mempool.space/tx/'+txid} target="_blank"
          rel="noopener noreferrer">{txid}</a>
      </p>}
      {stage==='submitted'&&txid&&<p role="status" className="privy-wallet-description">
        {de?'An Bitcoin-Netzwerk übermittelt':'Submitted to Bitcoin network'}:
        {' '}<a href={'https://mempool.space/tx/'+txid} target="_blank"
          rel="noopener noreferrer">{txid}</a>
      </p>}
      {lastSignedTxid&&lastSignedTxid!==txid&&<p role="status"
        className="privy-wallet-description">
        {de?'Letzte signierte BTC-Transaktion (Blockchain-Status im Explorer prüfen)':
          'Last signed BTC transaction (verify status on the explorer)'}:
        {' '}<a href={'https://mempool.space/tx/'+lastSignedTxid}
          target="_blank" rel="noopener noreferrer">{lastSignedTxid}</a>
      </p>}
    </div>}
  </section>;
}
