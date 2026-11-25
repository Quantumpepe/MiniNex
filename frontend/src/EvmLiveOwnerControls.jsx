import React,{useEffect,useState} from 'react';
import {useSigners,useSignMessage} from '@privy-io/react-auth';
import {
  authorizeCentralEvmWalletCoin,validCentralWalletStatus,
  centralWalletHasCoin,centralWalletCanExitCoin,
} from './centralEvmWalletFlow.js';
import {API_BASE,formatAssetQuantity} from './data.js';
import {
  LIVE_EVM_ASSETS,validEvmTradeChallenge,validEvmTradeSignerOffer,
  validEvmTradePermission,validEvmAutoSession,validEvmOrderHistory,
  validEvmFinalizedFills,
} from './evmLiveOwnerRules.js';

const EXPLORERS={1:'https://etherscan.io/tx/',56:'https://bscscan.com/tx/',
  137:'https://polygonscan.com/tx/',43114:'https://snowtrace.io/tx/',42161:'https://arbiscan.io/tx/'};
const WORDS={
  de:{
    title:'LIVE Handel',open:'Start Trade',
    desc:'Coin und Kapital einstellen, dann Start Trade drücken. MiniNex erledigt die technischen Schritte im Hintergrund. Die Wallet-Freigabe ist an dein Coin-Budget gebunden und gilt bis zum Widerruf. Pause/Stop stoppt neue Aufträge, nicht die Wallet-Freigabe.',
    status:'LIVE-Sitzung',setup:'Handel mit Privy verbinden',
    grant:'Privy-Handelsberechtigung freigeben',
    start:'LIVE starten',pause:'Handel pausieren',exit:'Stop & Exit',exiting:'Position wird geschlossen',stopped:'Beendet',
    warn:'Eine Privy-Freigabe umfasst den Handel für diesen Coin und das gewählte Kapital, die nötigen begrenzten Router-Freigaben sowie 5 % Gebühr auf realisierte Nettogewinne oberhalb des High-Water-Marks an die angezeigte MiniNex-Treasury. Keine Gewinngebühr bei Verlust. Netzwerkgebühren und Verluste sind möglich.',
    needBudget:'Zuerst Kapital und Stablecoin in den Handelseinstellungen speichern.',
    ready:'Freigabe verifiziert',unattached:'Wallet-Freigabe in Privy erforderlich',
    draft:'Policy bereit zur Veröffentlichung',publish:'Vorbereitete Policy veröffentlichen',idle:'Noch nicht verbunden',
    paused:'Pausiert',running:'Sitzung aktiv – Blockchain-Ausführung nur bei aktivem Backend-Worker',
    off:'Backend-Worker ist noch nicht als aktiv bestätigt. Ein Sitzungsstatus allein ist kein realer Trade.',
    revoked:'Widerrufen; alte Berechtigung nicht automatisch wieder aktivieren',
    failed:'Aktueller Status konnte nicht verifiziert werden.',
    error:'Die Privy-/Blockchain-Freigabe ist nicht vollständig bestätigt.',
    signing:'Wallet-Freigabe wird geprüft …',
    centralReady:'Eine gemeinsame Privy-Wallet-Freigabe ist verifiziert.',
    centralPending:'Der LIVE-Handel startet nur bei aktiver Coin-Sitzung und tatsächlich verifizierter Privy-Freigabe; ausgeführte Trades erscheinen erst nach Blockchain-Bestätigung.',
    centralRevoke:'Gemeinsame Wallet-Freigabe widerrufen',
    history:'LIVE-Aufträge',buy:'Kauf',sell:'Verkauf',
    empty:'Noch keine gesendeten echten Trades.',
    unproven:'Bestätigung offen',notSent:'Sendestatus noch nicht bestätigt',
    uncertain:'Sendeergebnis unklar',filled:'Swap finalisiert',
    native:'Nativer Handelszyklus bestätigt',failedOrder:'Swap fehlgeschlagen',
    fee:'5-%-Gebühr bezahlt',spent:'Eingesetzt',received:'Erhalten',
    recorded:'Auftrag erfasst',
    verifiedFill:'Blockchain-bestätigte Mengen',
    note:'Pausieren verhindert neue Trades, schliesst aber nicht automatisch eine offene Position. Bereits gesendete Transaktionen können nicht rückgängig gemacht werden.',
  },
  en:{
    title:'LIVE trading',open:'Start Trade',
    desc:'Set your coin and capital, then tap Start Trade. MiniNex handles the technical setup. Wallet authority is scoped to the selected coin budget until revoked. Pause/Stop prevents new orders; it does not revoke the wallet signer.',
    status:'LIVE session',setup:'Connect trading with Privy',
    grant:'Approve Privy trading signer',
    start:'Start LIVE',pause:'Pause trading',exit:'Stop & Exit',exiting:'Closing position',stopped:'Stopped',
    warn:'One Privy approval covers trading for this coin and chosen capital, finite router allowances and a 5% performance fee on realized net gains above the high-water mark paid to the disclosed MiniNex treasury. No profit fee on losses. Network fees and losses are possible.',
    needBudget:'Save your coin capital and stablecoin in trading settings first.',
    ready:'Grant verified',unattached:'Wallet grant required in Privy',
    draft:'Policy can be published',publish:'Publish prepared policy',idle:'Not connected',
    paused:'Paused',running:'Session active – blockchain execution requires an enabled backend worker',
    off:'Backend worker activation has not been confirmed. A session status does not prove a trade.',
    revoked:'Revoked; old permission cannot automatically reactivate',
    failed:'Current status could not be independently verified.',
    error:'Privy or blockchain permission not fully verified.',
    signing:'Checking wallet grant …',
    centralReady:'One combined Privy wallet grant has been verified.',
    centralPending:'LIVE trading requires an active coin session and verified Privy authorization; executed trades appear only after blockchain confirmation.',
    centralRevoke:'Revoke combined wallet authorization',
    history:'LIVE orders',buy:'Buy',sell:'Sell',
    empty:'No submitted real trades yet.',
    unproven:'Confirmation pending',notSent:'Submission not yet verified',
    uncertain:'Send result uncertain',filled:'Swap finalized',
    native:'Native trade cycle verified',failedOrder:'Swap reverted',
    fee:'5% fee paid',spent:'Spent',received:'Received',
    recorded:'Order recorded',
    verifiedFill:'Blockchain-confirmed amounts',
    note:'Pause prevents new trades but does not automatically sell an open position. Previously sent transactions cannot be undone.',
  },
};

export default function EvmLiveOwnerControls({
  asset,stable,address,userId,getAccessToken,enabled,lang='de',
}){
  const {addSigners,removeSigners}=useSigners();
  const {signMessage}=useSignMessage();
  const t=WORDS[lang==='de'?'de':'en'];
  const [permission,setPermission]=useState(null);
  const [centralStatus,setCentralStatus]=useState(null);
  const [session,setSession]=useState(null);
  const [orders,setOrders]=useState(null);
  const [fills,setFills]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [offeredFeeScope,setOfferedFeeScope]=useState(null);
  const allowed=enabled&&LIVE_EVM_ASSETS.includes(asset)&&
    /^0x[0-9a-fA-F]{40}$/.test(address||'')&&
    typeof getAccessToken==='function';
  const path='/api/live/evm';
  const request=async(route,method='GET',body)=>{
    const token=await getAccessToken();
    if(!token)throw Error('Privy sign-in required');
    const resp=await fetch(API_BASE+route,{
      method,cache:'no-store',
      headers:{
        Authorization:'Bearer '+token,Accept:'application/json',
        ...(body?{'Content-Type':'application/json'}:{}),
      },
      ...(body?{body:JSON.stringify(body)}:{}),
    });
    if(!resp.ok)throw Error('MiniNex LIVE EVM verification unavailable');
    return resp.json();
  };
  const refresh=async()=>{
    const [p,s,c]=await Promise.all([
      request(['QNT','AVAX'].includes(asset)
        ?path+'/wallet-policy/permission-status/'+asset
        :path+'/permission-status/'+asset),
      request(path+'/auto-session/'+asset),
      request(path+'/wallet-policy/status'),
    ]);
    if(!validEvmTradePermission(p,asset)||!validEvmAutoSession(s,asset)||
       !validCentralWalletStatus(c))
      throw Error('Inconsistent EVM LIVE trading state');
    setPermission(p);setSession(s);setCentralStatus(c);
    // Never await optional history before completing owner controls.
    void request(path+'/order-status/'+asset+'?limit=20')
      .then(h=>setOrders(validEvmOrderHistory(h,asset)?h:null))
      .catch(()=>setOrders(null));
    void request(path+'/verified-fills/'+asset+'?limit=20')
      .then(h=>setFills(validEvmFinalizedFills(h,asset)?h:null))
      .catch(()=>setFills(null));
  };
  useEffect(()=>{
    let active=true;
    setPermission(null);setSession(null);setCentralStatus(null);
    setOrders(null);setFills(null);
    setBusy(false);setError('');
    setOfferedFeeScope(null);
    if(!allowed)return()=>{active=false;};
    const poll=async()=>{
      try{
        const [p,s,c]=await Promise.all([
          request(['QNT','AVAX'].includes(asset)
        ?path+'/wallet-policy/permission-status/'+asset
        :path+'/permission-status/'+asset),
          request(path+'/auto-session/'+asset),
          request(path+'/wallet-policy/status'),
        ]);
        if(!validEvmTradePermission(p,asset)||!validEvmAutoSession(s,asset)||
           !validCentralWalletStatus(c))
          throw Error('Invalid backend trading session state');
        if(active){setPermission(p);setSession(s);setCentralStatus(c);setError('');}
        // Read-only history must never delay permission or session status.
        void request(path+'/order-status/'+asset+'?limit=20')
          .then(h=>{if(active)setOrders(validEvmOrderHistory(h,asset)?h:null);})
          .catch(()=>{if(active)setOrders(null);});
        void request(path+'/verified-fills/'+asset+'?limit=20')
          .then(h=>{if(active)setFills(validEvmFinalizedFills(h,asset)?h:null);})
          .catch(()=>{if(active)setFills(null);});
      }catch{if(active)setError(t.failed);}
    };
    void poll();
    // Passive read-only refresh: the backend heartbeat and genuine
    // blockchain submissions become visible without extra user clicks.
    // This never signs, starts, approves, or retries any transaction.
    const timer=window.setInterval(()=>{void poll();},20000);
    return()=>{active=false;window.clearInterval(timer);};
  },[asset,stable,address,userId,allowed]);
  const command=async(action)=>{
    if(!allowed||busy)return;
    setBusy(true);setError('');
    try{await action();}
    catch(e){setError(e?.message||t.error);}
    finally{
      try{await refresh();}catch{setError(t.failed);}
      setBusy(false);
    }
  };
  // One owner action orchestrates the existing durable and independently
  // verified sequence. This does not bypass either of Privy's actual
  // cryptographic approvals: a future consent protocol may consolidate them.
  // Never run on login, render, retry, or a background effect.
  const startTrade=()=>command(async()=>{
    const [freshPermission,freshSession,central]=await Promise.all([
      request(['QNT','AVAX'].includes(asset)
        ?path+'/wallet-policy/permission-status/'+asset
        :path+'/permission-status/'+asset),
      request(path+'/auto-session/'+asset),
      request(path+'/wallet-policy/status'),
    ]);
    if(!validEvmTradePermission(freshPermission,asset)||
       !validEvmAutoSession(freshSession,asset)||
       !validCentralWalletStatus(central))
      throw Error('Original wallet and trade session unavailable');
    if(freshSession.status==='ACTIVE')return;
    if(freshSession.status==='EXIT_PENDING')throw Error(t.exiting);
    // A new owner's first grant and every subsequent new EVM coin share
    // the SAME existing Privy signer and ONE policy. Existing legacy
    // grant records are never silently migrated or overwritten.
    if(central.status!=='NOT_CONFIGURED'||
       freshPermission.status==='NOT_PREPARED'){
      const current=await authorizeCentralEvmWalletCoin({
        asset,stable,address,request,addSigners,signMessage,
      });
      setCentralStatus(current);
      if(!centralWalletHasCoin(current,asset)){
        throw Error('Original combined EVM wallet grant is not verified');
      }
      const started=await request(
        path+'/wallet-policy/auto-session/'+asset+'/start','POST',
      );
      if(!validEvmAutoSession(started,asset)||
         started.status!=='ACTIVE'||started.started_by_owner!==true){
        throw Error('Original wallet LIVE session could not be started');
      }
      return;
    }
    let state=freshPermission.status;
    if(state==='REVOKED')throw Error(t.revoked);
    const mode=freshPermission.authorization_method;
    let published=freshPermission.policy_published===true;
    if(state==='NOT_PREPARED'){
      // Prepare exactly the user's saved budget and router policy, but
      // without requesting an EIP-191 message signature. At this stage
      // Privy grants NO spending authority whatsoever.
      const draft=await request(path+'/policy/'+asset+'/prepare-grant','POST');
      if(draft?.mode!=='OWNER_EVM_PRIVY_GRANT_DRAFT_ONLY'||
         draft?.asset!==asset||
         draft?.owner_delegation_granted!==false||
         draft?.policy_published_in_privy!==false||
         !Number.isSafeInteger(draft?.signed_budget_revision)||
         draft.signed_budget_revision<1)
        throw Error('Original scoped coin budget policy unavailable');
      state='DRAFT';
      published=false;
    }
    if(state==='DRAFT'&&!published){
      const publishedResult=await request(path+'/policy/'+asset+'/publish','POST');
      if(publishedResult?.status!=='REMOTE_VERIFIED_UNATTACHED'||
         publishedResult?.asset!==asset||
         publishedResult?.wallet_signer_attached!==false||
         publishedResult?.separate_privy_owner_add_signer_required!==true)
        throw Error('Uncertain wallet policy publication: no repeat permitted');
      published=true;
    }
    if((state==='DRAFT'&&published)||
       state==='CONSENT_VERIFIED'){
      const offer=await request(path+'/policy/'+asset+'/grant-review');
      if(!validEvmTradeSignerOffer(offer,{
        asset,stable,address,policyId:offer?.policy_id,
      }))throw Error('Privy owner policy differs from the saved coin budget');
      // Show the EXACT remotely audited recipient and fee ceiling before
      // the ONE wallet owner approval, without adding another confirmation.
      setOfferedFeeScope(offer.fee_scope);
      await new Promise(resolve=>window.requestAnimationFrame(resolve));
      // This is the ONE user-facing wallet approval. The Privy SDK shows
      // the request and the owner explicitly authorizes the exact scoped
      // signer. No prior EIP-191 signMessage is requested by this flow.
      await addSigners({
        address,signers:[{
          signerId:offer.execution_signer_id,
          policyIds:[offer.policy_id],
        }],
      });
      const checked=await request(path+'/policy/'+asset+'/confirm-grant','POST');
      if(checked?.state!=='GRANT_VERIFIED'||
         checked?.owner_signer_grant_verified!==true||
         checked?.transaction_signed_or_sent!==false)
        throw Error('Privy owner grant not independently verified');
      state='GRANT_VERIFIED';
    }
    if(state!=='GRANT_VERIFIED')
      throw Error('Trading authorization is not yet available');
    // An old UI state, stale budget or unrelated signer can never start.
    const beforeStart=await request(['QNT','AVAX'].includes(asset)
        ?path+'/wallet-policy/permission-status/'+asset
        :path+'/permission-status/'+asset);
    if(!validEvmTradePermission(beforeStart,asset)||
       beforeStart.status!=='GRANT_VERIFIED'||
       beforeStart.signer_grant_recorded!==true)
      throw Error('Trading authorization changed; no session was started');
    const result=await request(path+'/auto-session/'+asset+'/start','POST');
    if(!validEvmAutoSession(result,asset)||result.status!=='ACTIVE'||
       result.started_by_owner!==true)
      throw Error('LIVE session was not independently started');
  });
  const revokeCentral=()=>command(async()=>{
    if(!centralStatus||![
      'REMOTE_VERIFIED_READONLY','UPDATE_UNCERTAIN',
    ].includes(centralStatus.status))return;
    // One explicit action revokes the ONE original wallet signer for all
    // combined EVM coins. Backend GET must prove actual absence before
    // invalidating every local coin session.
    await removeSigners({address});
    const confirmed=await request(
      path+'/wallet-policy/confirm-revocation','POST',
    );
    if(confirmed?.status!=='REVOKED'||
       confirmed.wallet_signer_absence_verified!==true||
       confirmed.future_auto_orders_blocked!==true){
      throw Error('Privy signer removal requires independent verification');
    }
  });
  const pause=()=>command(async()=>{
    const result=await request(path+'/auto-session/'+asset+'/pause','POST');
    if(!validEvmAutoSession(result,asset)||result.status!=='PAUSED')
      throw Error('Cannot confirm per-coin pause');
  });
  const exitTrade=()=>command(async()=>{
    const result=await request(path+'/auto-session/'+asset+'/exit','POST');
    if(!validEvmAutoSession(result,asset)||result.status!=='EXIT_PENDING')
      throw Error('Cannot confirm per-coin exit request');
  });
  if(!allowed)return null;
  const current=permission?.status;
  const sessionStatus=session?.status;
  return <section className="privy-hwm-box" aria-label={t.title}
    style={{display:'grid',gap:8}}>
    <strong>{t.title} · {asset}</strong>
    <p className="privy-wallet-description">{t.desc}</p>
    {centralStatus?.status==='REMOTE_VERIFIED_READONLY'&&
      <p className="privy-wallet-description" role="status">
        {t.centralReady} {centralStatus.scope_coins.join(', ')}
        {' · '}{t.centralPending}
      </p>}
    <p className="privy-wallet-description" role="status">
      {t.status}: {sessionStatus==='ACTIVE'?
        (session?.worker_attached===true?t.running:t.off):
        sessionStatus==='PAUSED'?t.paused:
        sessionStatus==='EXIT_PENDING'?t.exiting:
        sessionStatus==='STOPPED'?t.stopped:
        current==='CONSENT_VERIFIED'?t.unattached:
        current==='GRANT_VERIFIED'?t.ready:
        current==='DRAFT'?t.draft:
        current==='REVOKED'?t.revoked:t.idle}
      {Number.isSafeInteger(session?.original_blockchain_swaps_sent)&&
        session.original_blockchain_swaps_sent>0&&
        <span> · {session.original_blockchain_swaps_sent} {lang==='de'?'Blockchain-Swaps gesendet':'blockchain swaps sent'}</span>}
      {Number.isSafeInteger(session?.original_blockchain_swaps_finalized)&&
        session.original_blockchain_swaps_finalized>0&&
        <span> · {session.original_blockchain_swaps_finalized} {lang==='de'?'erfolgreich finalisiert':'successfully finalized'}</span>}
    </p>
    {['ACTIVE','EXIT_PENDING'].includes(sessionStatus)
      ? <button type="button" className="privy-wallet-action"
          disabled={busy} onClick={pause}>{t.pause}</button>
      : current!=='REVOKED' &&
        <button type="button" className="privy-wallet-action"
          disabled={busy||permission===null||centralStatus===null} onClick={startTrade}>
          {t.open} · {asset}
        </button>}
    {['ACTIVE','PAUSED'].includes(sessionStatus)&&LIVE_EVM_ASSETS.includes(asset)&&
      centralWalletCanExitCoin(centralStatus,asset)&&
      <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
        disabled={busy} onClick={exitTrade}>{t.exit}</button>}
    {!['ACTIVE','EXIT_PENDING'].includes(sessionStatus)&&
      <p className="privy-wallet-description">{t.needBudget}</p>}
    {sessionStatus!=='ACTIVE'&&current!=='GRANT_VERIFIED'&&
      <p className="privy-wallet-description">{t.warn}</p>}
    <p className="privy-wallet-description">{t.note}</p>
    {orders&&<div className="mininex-live-orders" aria-label={t.history}
      style={{display:'grid',gap:6,maxHeight:260,overflowY:'auto',
        scrollbarWidth:'none'}}>
      <strong>{t.history} · {asset}</strong>
      {orders.orders.length===0&&<p className="privy-wallet-description">{t.empty}</p>}
      {orders.orders.map(order=><div key={order.intent_key}
        className="privy-wallet-description" style={{display:'grid',gap:2}}>
        <span>{order.side==='BUY'?t.buy:t.sell} · {order.stable}
          {' · '}{order.native_trade_cycle_settled?t.native:
            order.status==='FINALIZED_SUCCESS'?t.filled:
            order.status==='FINALIZED_REVERTED'?t.failedOrder:
            order.status==='SENDING'?t.notSent:
            order.status==='RECONCILIATION_REQUIRED'?t.uncertain:t.unproven}
          {order.fee_transferred?' · '+t.fee:''}
        </span>
        {fills?.fills?.find(fill=>fill.intent_key===order.intent_key&&
          fill.tx_hash===order.tx_hash)&&((fill)=>
          <span>{t.spent}: {formatAssetQuantity(fill.spent_amount,fill.spent_asset,lang)} {fill.spent_asset}
            {' · '}{t.received}: {formatAssetQuantity(fill.received_amount,fill.received_asset,lang)} {fill.received_asset}
          </span>)(fills.fills.find(fill=>fill.intent_key===order.intent_key&&
          fill.tx_hash===order.tx_hash))}
        {fills?.fills?.filter(fill=>fill.intent_key===order.intent_key&&fill.tx_hash===order.tx_hash&&
          fill.net_valuation?.net_verified===true).map(fill=>
          <span key={fill.tx_hash+'-net'}>
            {lang==='de'?'Nettoergebnis':'Net result'}: {Number(fill.net_valuation.realized_net_usd).toLocaleString(
              lang==='de'?'de-CH':'en-GB',{minimumFractionDigits:2,maximumFractionDigits:2})} USD
            {' · '}{fill.net_valuation.fee_payment_status==='PAID_VERIFIED'?
              (lang==='de'?'5%-Gebühr (bezahlt)':'5% fee (paid)'):
              (lang==='de'?'5%-Gebühr (offen)':'5% fee (due)')}: {formatAssetQuantity(
              String(Number(fill.net_valuation.fee_due_stable_atomic)/1000000),fill.stable,lang)} {fill.stable}
            {fill.net_valuation.fee_payment_tx_hash&&
              <a href={EXPLORERS[1]+fill.net_valuation.fee_payment_tx_hash} target="_blank" rel="noreferrer">
                {' · '}{lang==='de'?'Gebührentransaktion':'Fee transaction'}
              </a>}
          </span>)}
        <span>{t.recorded}: {new Date(order.created_at).toLocaleString(
          lang==='de'?'de-CH':'en-GB')}</span>
        {order.tx_hash&&EXPLORERS[order.chain_id]&&
          <a href={EXPLORERS[order.chain_id]+order.tx_hash} target="_blank" rel="noreferrer"
             style={{overflowWrap:'anywhere'}}>
            {order.tx_hash.slice(0,14)}…{order.tx_hash.slice(-8)}
          </a>}
        {fills?.fills?.filter(fill=>fill.intent_key===order.intent_key&&
          fill.ethereum_settlement_verified===true&&EXPLORERS[fill.destination_chain_id]).map(fill=>
          <a key={fill.destination_tx_hash} href={EXPLORERS[fill.destination_chain_id]+fill.destination_tx_hash}
             target="_blank" rel="noreferrer">
            {lang==='de'?'Zieltransaktion':'Destination transaction'} · {fill.destination_tx_hash.slice(0,10)}…
          </a>)}
      </div>)}
    </div>}
    {offeredFeeScope&&
      <p className="privy-wallet-description" role="status">
        {lang==='de'?'Einmalige Handelsfreigabe: 5 % auf positiven realisierten Nettogewinn':'Single trading grant: 5% of positive realized net profit'}
        {' · '}{lang==='de'?'Empfänger':'Recipient'}: <code>{offeredFeeScope.treasury_wallet}</code>
        {' · '}{lang==='de'?'Maximaler Transfer in Token-Atomeinheiten':'Maximum transfer in token atomic units'}: {offeredFeeScope.fee_max_atomic}
      </p>}
    {centralStatus&&['REMOTE_VERIFIED_READONLY','UPDATE_UNCERTAIN'].includes(centralStatus.status)&&
      <button type="button"
        className="privy-wallet-action privy-wallet-action--secondary"
        disabled={busy} onClick={revokeCentral}>{t.centralRevoke}</button>}
    {busy&&<p className="privy-wallet-description" role="status">{t.signing}</p>}
    {error&&<p className="privy-wallet-description" role="alert">{error}</p>}
  </section>;
}
