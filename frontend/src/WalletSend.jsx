import React,{useEffect,useRef,useState} from 'react';
import {useWallets} from '@privy-io/react-auth';
import {Send} from 'lucide-react';
import {COINS} from './coins.js';
import {SEND_ASSETS,sendAssetSpec,validSendAddress,exactSendUnits,
  formatSendUnits,buildEvmSendTransaction,readRpcQuantity,rpcHex,
  maxSendUnits,reviewEvmSend} from './walletSendRules.js';

// Manual transfers use the CURRENT user's own Privy EVM wallet. Never keep a
// server key, delegate signing or initiate a transaction without a user tap.
// Native BTC/SOL/XRP/TRX/DOGE/ADA sending is not claimed supported here.
export default function WalletSend({t,enabled,userId}) {
  const {wallets}=useWallets();
  const [open,setOpen]=useState(false);
  const [coin,setCoin]=useState('ETH');
  const [destination,setDestination]=useState('');
  const [amount,setAmount]=useState('');
  const [review,setReview]=useState(null);
  const [busy,setBusy]=useState(false);
  const [phase,setPhase]=useState('idle');
  const [txHash,setTxHash]=useState('');
  const [error,setError]=useState('');
  const requestId=useRef(0);
  const sending=useRef(false);
  const providerRef=useRef(null);
  const currentUser=useRef(userId);
  currentUser.current=userId;
  const embedded=wallets.find(w=>w?.walletClientType==='privy'&&
    /^0x[0-9a-fA-F]{40}$/.test(w?.address||''));
  const spec=sendAssetSpec(coin);

  const clearReview=()=>{
    requestId.current++;
    setReview(null);
    providerRef.current=null;
    setPhase('idle');
    setError('');
    setTxHash('');
  };
  useEffect(()=>{
    requestId.current++;
    sending.current=false;
    providerRef.current=null;
    setOpen(false);
    setReview(null);
    setDestination('');
    setAmount('');
    setPhase('idle');
    setTxHash('');
    setError('');
    setBusy(false);
  },[userId,enabled]);

  async function ownedProvider() {
    if(!enabled||!embedded||!spec||typeof embedded.getEthereumProvider!=='function'||
       typeof embedded.switchChain!=='function')throw Error('wallet');
    // A Privy EVM account is shared across supported EVM chains, but chain
    // selection must be checked before a send. No extra user authorization.
    await embedded.switchChain(spec.chainId);
    const provider=await embedded.getEthereumProvider();
    if(!provider||typeof provider.request!=='function')throw Error('wallet');
    const chain=readRpcQuantity(await provider.request({method:'eth_chainId'}));
    const accounts=await provider.request({method:'eth_accounts'});
    if(chain!==BigInt(spec.chainId)||!Array.isArray(accounts)||
       accounts.length!==1||
       typeof accounts[0]!=='string'||
       accounts[0].toLowerCase()!==embedded.address.toLowerCase())throw Error('wallet');
    return provider;
  }

  async function inspectFunds(provider) {
    const nativeBalance=readRpcQuantity(await provider.request({
      method:'eth_getBalance',params:[embedded.address,'latest'],
    }));
    let tokenBalance=null;
    if(spec.kind==='erc20'){
      const calldata='0x70a08231'+embedded.address.slice(2).toLowerCase().padStart(64,'0');
      const result=await provider.request({method:'eth_call',
        params:[{to:spec.contract,data:calldata},'latest']});
      if(typeof result!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(result))
        throw Error('balance');
      tokenBalance=BigInt(result);
    }
    return {nativeBalance,tokenBalance};
  }

  async function prepare(isMax=false,event) {
    event?.preventDefault();
    if(!enabled||busy||sending.current||!spec||
       !validSendAddress(destination,embedded?.address))return;
    const id=++requestId.current;
    const userAtStart=userId;
    setBusy(true);setPhase('reviewing');setError('');setReview(null);
    try{
      const provider=await ownedProvider();
      const funds=await inspectFunds(provider);
      const price=readRpcQuantity(await provider.request({method:'eth_gasPrice'}));
      if(price<=0n)throw Error('fee');
      // The price and limit supplied to Privy equal the bounds shown to the user.
      const gasPrice=(price*125n+99n)/100n;
      const atomic=isMax?1n:exactSendUnits(amount,spec.decimals);
      if(atomic===null)throw Error('amount');
      const transaction=buildEvmSendTransaction(
        coin,embedded.address,destination,atomic);
      const gas=readRpcQuantity(await provider.request({method:'eth_estimateGas',
        params:[transaction]}));
      const gasLimit=(gas*12n+9n)/10n;
      if(gasLimit>400000n||gasLimit<=0n)throw Error('fee');
      const spendable=spec.kind==='native'?funds.nativeBalance:funds.tokenBalance;
      const units=isMax?maxSendUnits(spendable,gasLimit,gasPrice,
        spec.kind==='native'):atomic;
      const exact=isMax?formatSendUnits(units,spec.decimals):amount;
      const record=reviewEvmSend({symbol:coin,from:embedded.address,
        to:destination,amount:exact,gasLimit,gasPrice,...funds});
      if(!record)throw Error('funds');
      if(id!==requestId.current||currentUser.current!==userAtStart)return;
      providerRef.current=provider;
      setAmount(exact);
      setReview(record);
      setPhase('ready');
    }catch{
      if(id===requestId.current){setError(t.walletSendReviewFailed);setPhase('failed');}
    }finally{if(id===requestId.current)setBusy(false);}
  }

  async function send() {
    if(!enabled||busy||sending.current||!review||!providerRef.current||
       Date.now()>=review.expiresAt||review.symbol!==coin||
       review.to!==destination||review.amount!==amount||
       review.from.toLowerCase()!==embedded?.address?.toLowerCase()||
       review.chainId!==spec?.chainId)return;
    sending.current=true;
    setBusy(true);setError('');setPhase('submitting');
    try{
      const provider=providerRef.current;
      const chain=readRpcQuantity(await provider.request({method:'eth_chainId'}));
      const accounts=await provider.request({method:'eth_accounts'});
      if(chain!==BigInt(review.chainId)||!Array.isArray(accounts)||
         accounts.length!==1||accounts[0]?.toLowerCase()!==review.from.toLowerCase())
        throw Error('account changed');
      const tx={...review.transaction,gas:rpcHex(review.gasLimit),
        gasPrice:rpcHex(review.gasPrice)};
      // The ONE user-initiated send call. Privy may display a mandatory
      // signing confirmation; no app-side delegated or automatic signature.
      const hash=await provider.request({method:'eth_sendTransaction',params:[tx]});
      if(typeof hash!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(hash))
        throw Error('uncertain broadcast');
      setTxHash(hash);
      setPhase('submitted');
    }catch(err){
      // A rejected wallet prompt is safe to retry with a FRESH fee review.
      if(err?.code===4001||err?.code==='ACTION_REJECTED'){
        setReview(null);setPhase('idle');sending.current=false;
        setError(t.walletSendRejected);
      }else{
        // A provider timeout may have broadcast. Never auto-repeat a transfer.
        setPhase('uncertain');setError(t.walletSendUncertain);
      }
    }finally{setBusy(false);}
  }

  async function verifyReceipt() {
    if(!txHash||!providerRef.current||busy)return;
    setBusy(true);
    try{
      const receipt=await providerRef.current.request({
        method:'eth_getTransactionReceipt',params:[txHash]});
      if(receipt===null){setPhase('submitted');return;}
      if(!receipt||receipt.transactionHash?.toLowerCase()!==txHash.toLowerCase()||
         receipt.to?.toLowerCase()!==review.transaction.to.toLowerCase()||
         receipt.from?.toLowerCase()!==review.from.toLowerCase()||
         !['0x0','0x1'].includes(receipt.status))throw Error('receipt');
      setPhase(receipt.status==='0x1'?'confirmed':'reverted');
    }catch{setError(t.walletSendReceiptFailed);}
    finally{setBusy(false);}
  }

  const change=(fn,value)=>{if(sending.current)return;clearReview();fn(value);};
  const labels=[...COINS.map(c=>({symbol:c.symbol,
    label:c.symbol+(c.inProgress?' · '+t.coinInProgress:'')})),
    {symbol:'USDC',label:'USDC'},{symbol:'USDT',label:'USDT'}];
  const explorer=review?.chainId===1?'https://etherscan.io/tx/':
    review?.chainId===56?'https://bscscan.com/tx/':
    review?.chainId===137?'https://polygonscan.com/tx/':
    review?.chainId===43114?'https://snowtrace.io/tx/':null;

  return <section className="mininex-wallet-send">
    <button type="button" className="privy-wallet-action mininex-swap-open"
      aria-expanded={open} aria-controls="mininex-send-form"
      onClick={()=>{if(sending.current)return;clearReview();setOpen(v=>!v);}}>
      <Send size={18} aria-hidden="true"/><span>{t.walletSendTitle}</span>
    </button>
    {open&&<div className="mininex-swap-content" id="mininex-send-form">
      <p className="privy-wallet-description">{t.walletSendManualOnly}</p>
      <form className="mininex-send-form" onSubmit={event=>prepare(false,event)}>
        <label><span>{t.walletSendAsset}</span>
          <select value={coin} disabled={busy||sending.current}
            onChange={e=>change(setCoin,e.target.value)}>
            {labels.filter(row=>SEND_ASSETS.includes(row.symbol)).map(row=>
              <option key={row.symbol} value={row.symbol}>{row.label}</option>)}
          </select>
        </label>
        <label><span>{t.walletSendRecipient}</span>
          <input value={destination} disabled={busy||sending.current}
            placeholder="0x…" autoComplete="off" spellCheck={false}
            onChange={e=>change(setDestination,e.target.value.trim())}/>
        </label>
        <label><span>{t.walletSendAmount} · {coin}</span>
          <input value={amount} disabled={busy||sending.current}
            inputMode="decimal" placeholder="0.00" maxLength={48}
            onChange={e=>change(setAmount,e.target.value)}/>
        </label>
        <div className="mininex-send-actions">
          <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
            disabled={!enabled||busy||sending.current||!spec||
              !validSendAddress(destination,embedded?.address)}
            onClick={event=>prepare(true,event)}>{t.walletSendMax}</button>
          <button type="submit" className="privy-wallet-action"
            disabled={!enabled||busy||sending.current||!spec||
              !validSendAddress(destination,embedded?.address)||
              exactSendUnits(amount,spec?.decimals)===null}>
            {busy&&phase==='reviewing'?t.walletSendReviewing:t.walletSendReview}
          </button>
        </div>
      </form>
      {!spec&&<p role="status" className="privy-wallet-description">
        {coin==='ADA'?t.walletSwapAdaPending:t.walletSendChainPending}
      </p>}
      {error&&<p role="alert" className="privy-wallet-description">{error}</p>}
      {review&&phase==='ready'&&<div className="mininex-swap-quote" role="status">
        <strong>{t.walletSendSummary}</strong>
        <div>{review.amount} {coin}</div>
        <div>{t.walletSendRecipient}: {review.to}</div>
        <div>{t.walletSendFeeMax}: {formatSendUnits(review.gasCost,18)} {spec.feeSymbol}</div>
        <p className="privy-wallet-description">{t.walletSendFinalNote}</p>
        <button className="privy-wallet-action" type="button"
          disabled={busy||Date.now()>=review.expiresAt}
          onClick={send}>{t.walletSendConfirm}</button>
      </div>}
      {['submitting','submitted','confirmed','reverted','uncertain'].includes(phase)&&
        <div className="mininex-swap-quote" role="status">
          <strong>{t['walletSendState'+phase]||t.walletSendUncertain}</strong>
          {txHash&&<div className="privy-wallet-description">
            {t.walletSendHash}: <code>{txHash}</code>
          </div>}
          {txHash&&explorer&&<a href={explorer+txHash}
            target="_blank" rel="noopener noreferrer">{t.walletSendExplorer}</a>}
          {txHash&&!['confirmed','reverted'].includes(phase)&&
            <button className="privy-wallet-action privy-wallet-action--secondary"
              type="button" disabled={busy} onClick={verifyReceipt}>
              {t.walletSendCheckReceipt}
            </button>}
        </div>}
    </div>}
  </section>;
}
