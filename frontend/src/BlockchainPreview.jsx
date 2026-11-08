import React, {useEffect, useState} from 'react';
import {usePrivy,useWallets} from '@privy-io/react-auth';
import {API_BASE,formatTimestamp} from './data.js';
import {PRIVY_APP_ID} from './privyConfig.js';

const BTC_ADDRESS=/^(?:bc1q[ac-hj-np-z02-9]{38,59}|bc1p[ac-hj-np-z02-9]{58}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$/;
const ETH_ADDRESS=/^0x[a-fA-F0-9]{40}$/;

function ExplorerView({t}) {
  const {ready,authenticated,user,getAccessToken}=usePrivy();
  const {wallets}=useWallets();
  const [asset,setAsset]=useState('BTC');
  const [cursor,setCursor]=useState(null);
  const [nextCursor,setNextCursor]=useState(null);
  const [syncStatus,setSyncStatus]=useState('idle');
  const [syncCount,setSyncCount]=useState(0);
  const [reload,setReload]=useState(0);
  const [state,setState]=useState('idle');
  const [items,setItems]=useState([]);
  const btc=user?.linkedAccounts?.find(account=>
    account?.type==='wallet' && account?.chainType==='bitcoin-segwit' &&
    BTC_ADDRESS.test(account?.address||''))?.address;
  const eth=wallets.find(wallet=>
    wallet?.walletClientType==='privy' && ETH_ADDRESS.test(wallet?.address||''))?.address;
  const address=asset==='BTC'?btc:eth;
  const changeAsset=next=>{setAsset(next);setCursor(null);setNextCursor(null);setItems([]);setSyncStatus('idle');};

  useEffect(()=>{
    if(!ready || !authenticated || !address){
      setItems([]);setState('idle');return;
    }
    let active=true;
    if(!cursor)setItems([]);
    setSyncStatus('idle');
    setState('loading');
    (async()=>{
      try {
        const token=await getAccessToken();
        if(!token)throw new Error('Login missing');
        const response=await fetch(API_BASE+'/api/blockchain/history/'+asset+
          '?address='+encodeURIComponent(address)+(cursor?'&cursor='+encodeURIComponent(cursor):''),{
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},
          cache:'no-store',
        });
        if(!response.ok)throw new Error('Explorer unavailable');
        const data=await response.json();
        if(!data?.preview_only || data.wallet_ownership_verified!==false ||
           data.tax_complete!==false || data.asset!==asset ||
           data.address!==address || !Array.isArray(data.items))
          throw new Error('Unexpected explorer response');
        if(active){setItems(old=>cursor?[...old,...data.items.filter(x=>!old.some(y=>y.id===x.id && y.asset===x.asset))]:data.items);setNextCursor(data.next_cursor||null);setState('ready');}
      }catch{if(active){setItems([]);setState('failed');}}
    })();
    return()=>{active=false;};
  },[ready,authenticated,asset,address,getAccessToken,reload,cursor]);

  const archivePage=async()=>{
    if(!address || !authenticated || state!=='ready' || syncStatus==='saving')return;
    setSyncStatus('saving');
    try{
      const token=await getAccessToken();
      if(!token)throw new Error('Not authenticated');
      const response=await fetch(API_BASE+'/api/blockchain/sync/'+asset+
        '?address='+encodeURIComponent(address)+(cursor?'&cursor='+encodeURIComponent(cursor):''),{
        method:'POST',headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        cache:'no-store'
      });
      if(!response.ok)throw new Error('Verification unavailable');
      const data=await response.json();
      if(data?.wallet_ownership_verified!==true || data?.tax_complete!==false ||
          data?.asset!==asset || !Number.isInteger(data?.saved))
        throw new Error('Unexpected verification result');
      setSyncCount(data.saved);
      setSyncStatus('saved');
      window.dispatchEvent(new Event('mininex-activity-updated'));
    }catch{setSyncStatus('failed');}
  };

  return <section className="panel activity-report">
    <div className="activity-report-heading">
      <h2>{t.chainPreviewTitle}</h2>
      <div className="tax-export-actions">
        {['BTC','ETH','USDC','USDT'].map(coin=><button key={coin} type="button" aria-pressed={asset===coin}
          className={asset===coin?'chain-tab-selected':''}
          onClick={()=>changeAsset(coin)}>{coin}</button>)}
      </div>
    </div>
    <p>{t.chainPreviewNote}</p>
    {!ready?<p>{t.privyLoading}</p>:
      !authenticated?<p>{t.taxLoginRequired}</p>:
      !address?<p>{t.chainWalletMissing}</p>:<>
        <button className="privy-wallet-action privy-wallet-action--secondary"
          type="button" disabled={state==='loading'} onClick={()=>{setCursor(null);setNextCursor(null);setReload(n=>n+1);}}>
          {t.chainRefresh}
        </button>
        {state==='loading'&&<p>{t.taxLoading}</p>}
        {state==='failed'&&<p role="alert">{t.chainPreviewUnavailable}</p>}
        {state==='ready'&&!items.length&&<p>{t.chainNoTransfers}</p>}
        {items.length>0&&<div className="tax-activity-list">
          {items.map(tx=><div key={tx.id} className="tax-activity-row">
            <div>
              <strong>{tx.direction==='IN'?t.chainIncoming:t.chainOutgoing} · {tx.asset}</strong>
              <small>{tx.timestamp?formatTimestamp(tx.timestamp,undefined,{year:'numeric',month:'2-digit',day:'2-digit'}):t.chainPending}</small>
              <small>{tx.confirmed?t.chainConfirmed:t.chainPending}</small>
            </div>
            <div>
              <strong>{tx.quantity} {tx.asset}</strong>
              <a href={tx.explorer_url} target="_blank" rel="noopener noreferrer">
                {t.chainOpenExplorer}
              </a>
            </div>
          </div>)}
        </div>}
        {state==='ready'&&<button type="button" className="privy-wallet-action privy-wallet-action--secondary"
          onClick={archivePage} disabled={syncStatus==='saving'}>
          {t.chainArchivePage}
        </button>}
        {syncStatus==='saving'&&<p>{t.chainArchiving}</p>}
        {syncStatus==='saved'&&<p>{t.chainArchived.replace('{count}',String(syncCount))}</p>}
        {syncStatus==='failed'&&<p role="alert">{t.chainArchiveUnavailable}</p>}
        {state==='ready'&&nextCursor&&<button type="button" className="privy-wallet-action privy-wallet-action--secondary"
          onClick={()=>setCursor(nextCursor)}>{t.chainLoadMore}</button>}
      </>}
  </section>;
}

export default function BlockchainPreview({t}) {
  if(!PRIVY_APP_ID)return null;
  return <ExplorerView t={t}/>;
}
