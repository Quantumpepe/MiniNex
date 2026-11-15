import React,{Suspense,useState} from 'react';
import {Send} from 'lucide-react';

// Bitcoin's crypto implementation must NOT enter the MiniNex startup bundle.
// The user must tap BTC Send before React imports any Bitcoin libraries.
const BtcSendFeature=React.lazy(()=>import('./BtcWalletSend.jsx'));

class BtcSendErrorBoundary extends React.Component {
  constructor(props){super(props);this.state={failed:false};}
  static getDerivedStateFromError(){return {failed:true};}
  render(){
    if(this.state.failed)return <p role="alert" className="privy-wallet-description">
      {this.props.lang==='de'
        ?'BTC-Senden kann auf diesem Gerät nicht geladen werden. MiniNex läuft weiter; es wurde nichts versendet.'
        :'BTC sending is unavailable on this device. MiniNex continues to run; nothing was sent.'}
    </p>;
    return this.props.children;
  }
}
export default function BtcSendEntry({t,lang='de',userId,wallet,enabled}) {
  const [requested,setRequested]=useState(false);
  return <div className="mininex-btc-send-entry">
    {!requested&&<button type="button" className="privy-wallet-action mininex-swap-open"
      disabled={!enabled}
      onClick={()=>setRequested(true)}
      aria-label={lang==='de'?'BTC senden öffnen':'Open BTC send'}>
      <Send size={18} aria-hidden="true"/>{t.walletSendTitle} BTC
    </button>}
    {requested&&<BtcSendErrorBoundary lang={lang}>
      <Suspense fallback={<p role="status" className="privy-wallet-description">
        {lang==='de'?'BTC-Senden wird geladen…':'Loading BTC send…'}
      </p>}>
        <BtcSendFeature t={t} lang={lang} userId={userId}
          wallet={wallet} enabled={enabled}/>
      </Suspense>
    </BtcSendErrorBoundary>}
  </div>;
}
