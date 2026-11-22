import React,{useState} from 'react';
import {Send} from 'lucide-react';

export default function NativeSendEntry({t,asset,enabled}) {
  const [open,setOpen]=useState(false);
  const labels={TRX:'TRON',XRP:'XRP Ledger',DOGE:'Dogecoin'};
  return <section className="mininex-wallet-send">
    <button type="button" className="privy-wallet-action" onClick={()=>setOpen(v=>!v)} disabled={!enabled}>
      <Send size={16}/> {t.walletSendTitle||'Senden'} {asset}
    </button>
    {open&&<div className="mininex-send-content">
      <p className="privy-wallet-description">
        {t.nativeSendPreparing||(asset+'-Send wird vorbereitet. Bitte später erneut versuchen.')}
      </p>
    </div>}
  </section>;
}
