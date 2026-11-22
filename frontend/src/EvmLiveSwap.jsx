import React,{useState} from 'react';
import {useWallets} from '@privy-io/react-auth';
import {buildEvmSwapTransaction} from './evmSwapBuilder.js';
import {EVM_CHAINS} from './evmSwapRules.js';

// Universal EVM swap: works for BNB, POL, AVAX, ARB.
export default function EvmLiveSwap({enabled,userId,chain,side,stable,amount,t}){
  const {wallets}=useWallets();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [hash,setHash]=useState('');
  const wallet=wallets.find(w=>w?.walletClientType==='privy'&&
    /^0x[0-9a-fA-F]{40}$/.test(w?.address||''));
  const config=EVM_CHAINS[chain];

  async function executeSwap(){
    if(!wallet||!amount||busy||!config)return;
    setBusy(true);setError('');setHash('');
    try{
      const provider=await wallet.getEthereumProvider();
      const amountIn=BigInt(Math.floor(Number(amount)*1e18)).toString();
      const deadline=Math.floor(Date.now()/1000)+600;
      const tx=buildEvmSwapTransaction(chain,{
        side,amountIn,amountOutMin:'0',stable,
        recipient:wallet.address,deadline,
      });
      const txHash=await provider.request({
        method:'eth_sendTransaction',
        params:[{
          from:wallet.address,to:tx.to,data:tx.data,
          value:tx.value,gas:tx.gasLimit,gasPrice:tx.gasPrice,
        }],
      });
      setHash(txHash);
    }catch(e){
      setError(e.message||'Swap failed');
    }finally{
      setBusy(false);
    }
  }

  if(!enabled||!wallet||!config)return null;
  return <div className="mininex-evm-swap">
    <button type="button" className="privy-wallet-action" disabled={busy||!amount}
      onClick={executeSwap}>
      {busy?'Signiere...':'Swap starten'}
    </button>
    {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
    {hash&&<p className="privy-wallet-description">TX: {hash.slice(0,10)}...</p>}
  </div>;
}
