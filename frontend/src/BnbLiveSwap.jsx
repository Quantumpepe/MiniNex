import React,{useState,useRef} from 'react';
import {useWallets} from '@privy-io/react-auth';
import {buildBnbSwapTransaction} from './bnbSwapBuilder.js';

// Simple BNB swap: user selects, enters amount, signs.
export default function BnbLiveSwap({enabled,userId,side,stable,amount,t}){
  const {wallets}=useWallets();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [hash,setHash]=useState('');
  const wallet=wallets.find(w=>w?.walletClientType==='privy'&&
    /^0x[0-9a-fA-F]{40}$/.test(w?.address||''));

  async function executeSwap(){
    if(!wallet||!amount||busy)return;
    setBusy(true);setError('');setHash('');
    try{
      const provider=await wallet.getEthereumProvider();
      const amountIn=BigInt(Math.floor(Number(amount)*1e18)).toString();
      const deadline=Math.floor(Date.now()/1000)+600; // 10 min
      const tx=buildBnbSwapTransaction({
        side,
        amountIn,
        amountOutMin:'0', // no slippage protection for now
        stable,
        recipient:wallet.address,
        deadline,
      });
      const txHash=await provider.request({
        method:'eth_sendTransaction',
        params:[{
          from:wallet.address,
          to:tx.to,
          data:tx.data,
          value:tx.value,
          gas:tx.gasLimit,
          gasPrice:tx.gasPrice,
        }],
      });
      setHash(txHash);
    }catch(e){
      setError(e.message||'Swap failed');
    }finally{
      setBusy(false);
    }
  }

  if(!enabled||!wallet)return null;
  return <div className="mininex-bnb-swap">
    <button type="button" className="privy-wallet-action" disabled={busy||!amount}
      onClick={executeSwap}>
      {busy?'Signiere...':'Swap starten'}
    </button>
    {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
    {hash&&<p className="privy-wallet-description">TX: {hash.slice(0,10)}...</p>}
  </div>;
}
