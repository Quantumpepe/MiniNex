import React, {useState} from 'react';
import {usePrivy, useWallets} from '@privy-io/react-auth';
import {LockKeyhole, ShieldCheck, Wallet} from 'lucide-react';
import {PRIVY_APP_ID} from './privyConfig.js';

// Deliberately no sendTransaction, withdrawal or delegation path here.
// The existing backend is PAPER-only, even when a user signs in to Privy.
function ConnectedPrivyWallet({t}) {
  const {ready, authenticated, login, logout} = usePrivy();
  const {wallets, ready:walletsReady} = useWallets();
  const [error,setError]=useState(false);
  const embedded = wallets.find(wallet=>wallet.walletClientType==='privy' && /^0x[0-9a-fA-F]{40}$/.test(wallet.address||''));
  const address = embedded?.address;
  const act = async (fn) => {try {setError(false); await fn();} catch {setError(true);}};
  return <section className="panel privy-panel">
    <div className="privy-panel-header"><h2>{t.privyTitle}</h2><ShieldCheck size={19} color="#54eabb"/></div>
    <div className="privy-panel-body">
      {!ready ? <div className="privy-wallet-description">{t.privyLoading}</div> :
        !authenticated ? <>
          <div className="privy-wallet-description">{t.privyNoDeposits}</div>
          <button className="privy-wallet-action" type="button" onClick={()=>act(login)}>{t.privyLogin}</button>
        </> : <>
          <div className="privy-wallet-description">{address?t.privyConnected:(walletsReady?t.privyNoWallet:t.privyLoading)} · {t.privyEvm}</div>
          {address&&<div className="privy-wallet-address" title={t.privyEvm}>{address.slice(0,12)}…{address.slice(-10)}</div>}
          <div className="privy-wallet-note"><LockKeyhole size={13} style={{verticalAlign:'middle',marginRight:6}}/>{t.privyNoDeposits}</div>
          <button className="privy-wallet-action privy-wallet-action--secondary" type="button" onClick={()=>act(logout)}>{t.privyLogout}</button>
        </>}
      {error&&<p role="alert" className="privy-wallet-note">{t.privyError}</p>}
    </div>
  </section>;
}
export default function PrivyWalletPanel({t}) {
  if(!PRIVY_APP_ID) return <section className="panel privy-panel">
    <div className="privy-panel-header"><h2>{t.privyTitle}</h2><Wallet size={19}/></div>
    <div className="privy-panel-body">
      <strong>{t.privyUnavailable}</strong>
      <p className="privy-wallet-description">{t.privyUnavailableNote}</p>
      <p className="privy-wallet-note">{t.privyNoDeposits}</p>
    </div>
  </section>;
  return <ConnectedPrivyWallet t={t}/>;
}
