import React,{useEffect,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE,formatTimestamp} from './data.js';
import {PRIVY_APP_ID} from './privyConfig.js';

// One public mainnet at a time, including EVM token activity (e.g. WPOL).
// The server reads wallet addresses independently from authenticated Privy.
const NETWORKS=['BTC','ETH','BNB','POL','AVAX','ARB','SOL','XRP','TRX'];
const HOSTS={
  BTC:'mempool.space',ETH:'etherscan.io',BNB:'bscscan.com',
  POL:'polygonscan.com',AVAX:'snowtrace.io',ARB:'arbiscan.io',
  SOL:'solscan.io',XRP:'xrpscan.com',TRX:'tronscan.org',
};

export function verifiedExplorerUrl(chain,url,kind='tx'){
  if(!HOSTS[chain]||typeof url!=='string'||url.length>240)return null;
  try{
    const u=new URL(url);
    if(u.protocol!=='https:'||u.username||u.password||
       u.port||u.hostname!==HOSTS[chain])return null;
    const full=u.pathname+u.hash;
    if(!/^[A-Za-z0-9_/#.-]+$/.test(full))return null;
    if(kind==='address'){
      return (/\/(address|account)\//.test(full)&&
        !u.search)?u.href:null;
    }
    return (/\/(tx|transaction)\//.test(full)&&
      !u.search)?u.href:null;
  }catch{return null;}
}

const amountLabel=tx=>tx?.quantity&&tx?.asset
  ? tx.quantity+' '+tx.asset:null;

function NetworkActivity({t,lang}){
  const {ready,authenticated,user,getAccessToken,login}=usePrivy();
  const [chain,setChain]=useState('POL');
  const [refresh,setRefresh]=useState(0);
  const [data,setData]=useState(null);
  const [state,setState]=useState('idle');
  useEffect(()=>{
    const controller=new AbortController();
    let active=true;
    setData(null);
    if(!ready||!authenticated||!user?.id){
      setState('idle');return()=>controller.abort();
    }
    setState('loading');
    (async()=>{
      try{
        const token=await getAccessToken();
        if(!active||!token)throw Error('Not signed in');
        const response=await fetch(API_BASE+
          '/api/blockchain/chain-transactions/'+chain,{
          method:'GET',cache:'no-store',signal:controller.signal,
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        });
        if(!response.ok)throw Error('History unavailable');
        const result=await response.json();
        if(result?.chain!==chain||
           result?.wallet_ownership_verified!==true||
           result?.preview_only!==true||
           result?.tax_complete!==false||
           result?.history_complete!==false||
           !['ready','partial','unavailable','wallet_missing'].includes(result.source_status)||
           !Array.isArray(result.items)||
           result.items.length>40||
           result.items.some(tx=>tx?.chain!==chain||
             !verifiedExplorerUrl(chain,tx.explorer_url))||
           (result.explorer_address_url!=null&&
             !verifiedExplorerUrl(chain,result.explorer_address_url,'address')))
          throw Error('Unverified chain activity');
        if(active){setData(result);setState('ready');}
      }catch{
        if(active&&!controller.signal.aborted){setState('failed');setData(null);}
      }
    })();
    return()=>{active=false;controller.abort();};
  },[ready,authenticated,user?.id,getAccessToken,chain,refresh]);

  return <section className="panel activity-report" aria-label={t.chainNetworkTitle}>
    <div className="activity-report-heading">
      <h2>{t.chainNetworkTitle}</h2>
      <select aria-label={t.chainNetworkSelect} value={chain}
        onChange={e=>setChain(e.target.value)}>
        {NETWORKS.map(c=><option key={c} value={c}>{c}</option>)}
      </select>
    </div>
    <p>{t.chainNetworkNote}</p>
    {!ready?<p>{t.privyLoading}</p>:
      !authenticated?<button type="button" className="privy-wallet-action"
        onClick={login}>{t.privyLogin}</button>:<>
        <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
          onClick={()=>setRefresh(n=>n+1)} disabled={state==='loading'}>
          {t.chainRefresh}
        </button>
        {state==='loading'&&<p role="status">{t.chainNetworkLoading}</p>}
        {state==='failed'&&<p role="alert">{t.chainNetworkUnavailable}</p>}
        {state==='ready'&&data?.source_status==='wallet_missing'&&
          <p>{t.chainNetworkWalletMissing}</p>}
        {state==='ready'&&data?.source_status==='unavailable'&&
          <p role="status">{t.chainNetworkUnavailable}</p>}
        {state==='ready'&&data?.source_status==='partial'&&
          <p role="status">{t.chainNetworkPartial}</p>}
        {state==='ready'&&data?.source_status==='ready'&&
          !data.items.length&&<p>{t.chainNetworkEmpty}</p>}
        {state==='ready'&&data?.items.length>0&&
          <div className="tax-activity-list">
            {data.items.map(tx=><a key={chain+':'+tx.hash}
              className="tax-activity-row chain-history-transaction"
              style={{display:'flex',gap:14,textDecoration:'none',
                color:'inherit',minWidth:0,alignItems:'center'}}
              href={verifiedExplorerUrl(chain,tx.explorer_url)}
              target="_blank" rel="noopener noreferrer"
              aria-label={t.chainNetworkOpen+' '+chain+' '+tx.hash}>
              <div style={{minWidth:0,flex:1}}>
                <strong>{tx.type==='WRAP'?t.chainNetworkWrap+' '+chain+' → W'+chain:
                  (tx.label||t.chainNetworkTransaction)}</strong>
                <small>{tx.timestamp?formatTimestamp(tx.timestamp,lang,{
                  year:'numeric',month:'2-digit',day:'2-digit'}):t.chainPending}
                  {' · '}{tx.failed?t.chainNetworkFailed:
                    tx.confirmed?t.chainConfirmed:t.chainPending}</small>
                <small style={{overflowWrap:'anywhere'}}>
                  {tx.hash.slice(0,12)+'…'+tx.hash.slice(-8)}
                </small>
                {tx.tokens?.length>0&&<small style={{overflowWrap:'anywhere'}}>
                  {tx.tokens.slice(0,6).map(token=>
                    (token.direction==='IN'?'+':'−')+
                    token.quantity+' '+token.asset).join(' · ')}
                </small>}
              </div>
              <div style={{flexShrink:0,textAlign:'right'}}>
                {amountLabel(tx)&&<strong>{amountLabel(tx)}</strong>}
                <small>{t.chainNetworkOpen} ↗</small>
              </div>
            </a>)}
          </div>}
        {state==='ready'&&data?.explorer_address_url&&
          <p><a href={verifiedExplorerUrl(chain,data.explorer_address_url,'address')}
            target="_blank" rel="noopener noreferrer">{t.chainNetworkFull} ↗</a></p>}
        {state==='ready'&&data?.source_status!=='wallet_missing'&&
          <small>{t.chainNetworkLimited}</small>}
      </>}
  </section>;
}

export default function ChainTransactions({t,lang}){
  if(!PRIVY_APP_ID)return null;
  return <NetworkActivity t={t} lang={lang}/>;
}
