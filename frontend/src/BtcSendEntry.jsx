import React,{Suspense,useState} from 'react';
import {Send} from 'lucide-react';

// Keep Bitcoin cryptography outside the initial Android WebView bundle.
const loadBtcSend=()=>import('./BtcWalletSend.jsx');
const BtcSendFeature=React.lazy(loadBtcSend);

function classifyLoadError(error) {
  const message=String(error?.message||error||'');
  if(/Failed to fetch dynamically imported module|Importing a module script failed|ChunkLoadError|Loading chunk|Failed to load module script/i.test(message))
    return 'chunk-load';
  if(/Buffer is not defined|process is not defined|global is not defined/i.test(message))
    return 'runtime-polyfill';
  if(/Cannot read properties|undefined is not|is not a function|Invalid hook call/i.test(message))
    return 'runtime';
  return 'unknown';
}

class BtcSendErrorBoundary extends React.Component {
  constructor(props){super(props);this.state={failed:false,reason:null};}
  static getDerivedStateFromError(error){
    return {failed:true,reason:classifyLoadError(error)};
  }
  componentDidCatch(error,info){
    // Error details remain in the device console; don't log wallet data.
    console.error('[MiniNex BTC Send] load/render failure',error,info?.componentStack);
  }
  render(){
    if(this.state.failed){
      const de=this.props.lang==='de';
      return <div className="mininex-btc-send-load-error">
        <p role="alert" className="privy-wallet-description">
          {de
            ?'BTC-Senden konnte nicht geöffnet werden. MiniNex läuft weiter; es wurde nichts versendet.'
            :'BTC sending could not open. MiniNex continues running; nothing was sent.'}
        </p>
        <p className="privy-wallet-description">
          {de?'Fehlercode':'Error code'}: BTC-SEND-{this.state.reason}
        </p>
        <button type="button" className="privy-wallet-action"
          onClick={this.props.onRetry}>
          {de?'Erneut versuchen':'Try again'}
        </button>
      </div>;
    }
    return this.props.children;
  }
}
export default function BtcSendEntry({t,lang='de',userId,wallet,enabled}) {
  const [requested,setRequested]=useState(false);
  const [attempt,setAttempt]=useState(0);
  const retry=()=>{setRequested(false);setAttempt(value=>value+1);};
  return <div className="mininex-btc-send-entry">
    {!requested&&<button type="button" className="privy-wallet-action mininex-swap-open"
      disabled={!enabled}
      onClick={()=>setRequested(true)}
      aria-label={lang==='de'?'BTC senden öffnen':'Open BTC send'}>
      <Send size={18} aria-hidden="true"/>{t.walletSendTitle} BTC
    </button>}
    {requested&&<BtcSendErrorBoundary key={attempt} lang={lang} onRetry={retry}>
      <Suspense fallback={<p role="status" className="privy-wallet-description">
        {lang==='de'?'BTC-Senden wird geladen…':'Loading BTC send…'}
      </p>}>
        <BtcSendFeature t={t} lang={lang} userId={userId}
          wallet={wallet} enabled={enabled}/>
      </Suspense>
    </BtcSendErrorBoundary>}
  </div>;
}
