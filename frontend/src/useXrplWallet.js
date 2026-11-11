import {useCreateWallet as useCreateExtendedWallet} from '@privy-io/react-auth/extended-chains';
import {useEffect,useRef,useState} from 'react';
import {linkedVerifiedNativeAddress,validatedCreatedXrplAddress} from './xrpAdaWalletAddresses.js';

// Native XRP Ledger wallet, strictly tied to one verified Privy login.
// Creation may not be available for every Privy SDK rollout/account yet.
// If XRPL support is missing or an address cannot be verified, FAIL CLOSED:
// do not show a receiving address and do not authorize deposits or transfers.
export function useXrplWallet(user, enabled) {
  const {createWallet} = useCreateExtendedWallet();
  const walletCreator=useRef(createWallet);
  walletCreator.current=createWallet;
  const [created,setCreated] = useState(null);
  const [phase,setPhase] = useState({userId:null,name:'checking'});
  const [retry,setRetry] = useState(0);
  const attemptKey = useRef(null);
  const account = useRef(null);
  account.current={userId:user?.id,enabled};
  const userId=user?.id;
  const stored=enabled&&created?.userId===userId?created.address:null;
  // A Privy account refresh can return a new linkedAccounts array on every
  // render. Depend on stable XRPL record contents, not array identity: otherwise
  // a refresh can cancel a wallet creation while its promise is in flight.
  const linkedXrpl=Array.isArray(user?.linkedAccounts)
    ?user.linkedAccounts.filter(a=>a?.type==='wallet'&&a?.chainType==='xrpl')
    :[];
  const hasLinkedXrpl=linkedXrpl.length>0;
  // Creating a new XRPL key is NOT idempotent across multiple devices.
  // Keep account-scoped creation disabled in normal releases until actual
  // Privy XRPL SDK support AND a server-side creation lock are confirmed.
  // Existing verified, linked XRPL addresses may still be displayed.
  const creationVerifiedAndEnabled=
    import.meta.env?.VITE_MININEX_XRPL_CREATION_VERIFIED==='true';
  const linkedXrplKey=JSON.stringify(linkedXrpl.map(a=>[
    a.walletClientType,a.address,
  ]).sort((x,y)=>String(x[1]).localeCompare(String(y[1]))));

  useEffect(()=>{
    let active=true;
    if(!enabled||!userId)return ()=>{active=false;};
    const belongs=()=>active&&account.current.userId===userId&&account.current.enabled;
    (async()=>{
      // Verify linked wallet chain and checksum independently; do NOT accept
      // an XRPL-looking EVM/Solana/wrong-account address.
      const linked=await linkedVerifiedNativeAddress(user,'xrpl');
      if(!belongs())return;
      if(linked){
        setCreated({userId,address:linked});
        setPhase({userId,name:'created'});
        return;
      }
      // An invalid preexisting XRPL wallet must be examined, never silently
      // replaced by a second address or presented as a funded account.
      if(hasLinkedXrpl){
        setPhase({userId,name:'invalid'});
        return;
      }
      if(stored){
        setPhase({userId,name:'created'});
        return;
      }
      if(!creationVerifiedAndEnabled){
        setPhase({userId,name:'checking'});
        return;
      }
      const attempt=userId+':xrpl:'+retry;
      if(attemptKey.current===attempt){
        // A previous create may have completed after an auth transition.
        // Refuse to silently create a second wallet; require account sync.
        setPhase({userId,name:'invalid'});
        return;
      }
      attemptKey.current=attempt;
      setPhase({userId,name:'creating'});
      // This SDK call can reject when XRPL provisioning has not yet rolled
      // out. Reject safely and allow an explicit retry, no fallback keypair.
      const response=await walletCreator.current({chainType:'xrpl'});
      if(!belongs())return;
      const address=await validatedCreatedXrplAddress(response,userId);
      if(!address){
        // If a wallet was created but its provenance/address failed verification,
        // never create a second wallet on a retry. Manual operator review required.
        setPhase({userId,name:'invalid'});
        return;
      }
      if(!belongs())return;
      setCreated({userId,address});
      setPhase({userId,name:'created'});
    })().catch(()=>{
      if(belongs())setPhase({userId,name:'failed'});
    });
    return()=>{active=false;};
  },[enabled,userId,linkedXrplKey,retry,stored,hasLinkedXrpl,creationVerifiedAndEnabled]);

  const address=enabled&&created?.userId===userId?created.address:null;
  return {
    address,
    state:!enabled?'checking':address?'created':
      phase.userId===userId?phase.name:'checking',
    retry:()=>setRetry(n=>n+1),
  };
}
