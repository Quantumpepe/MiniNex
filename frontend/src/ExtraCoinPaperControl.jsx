import {formatMoney,formatSigned as formatDisplaySigned} from './data.js';
import React,{useCallback,useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {validExtraPaperState,extraPaperCanStart,liveOwnerSessionPath,
  validLiveOwnerSession,liveOwnerCounters} from './extraPaperRules.js';

// One opt-in server-side PAPER session per coin and account. Its simulation
// persists when Android closes; no wallet, live orders, approvals or deposits.
export default function ExtraCoinPaperControl({asset,t,enabled,getAccessToken,
                                               options,onChange,liveMode=false}){
  const [data,setData]=useState(null);
  const [busy,setBusy]=useState(false);
  const [failed,setFailed]=useState(false);
  const [loading,setLoading]=useState(true);
  const activeRef=useRef(0);
  const busyRef=useRef(false);
  const mutationRef=useRef(0);

  const fetchOne=useCallback(async(signal)=>{
    const token=await getAccessToken();
    if(!token)throw Error('Login required');
    const paperPath=['BTC','ETH'].includes(asset)?'/api/user-paper/':'/api/extra-paper/';
    const livePath=liveMode?liveOwnerSessionPath(asset):null;
    if(liveMode&&!livePath)throw Error('Unsupported LIVE coin');
    const response=await fetch(API_BASE+(liveMode?livePath:paperPath+asset),{
      method:'GET',
      headers:{Authorization:'Bearer '+token,Accept:'application/json'},
      cache:'no-store',signal,
    });
    if(!response.ok)throw Error(liveMode?'Live session unavailable':'Personal paper state unavailable');
    const result=await response.json();
    if(liveMode&&!validLiveOwnerSession(result,asset))
      throw Error('Unverified LIVE session status');
    // BTC/ETH user-paper has different format; normalize for display
    if(['BTC','ETH'].includes(asset)&&result.position){
      // Server returns persisted virtual start capital as a decimal string.
      // A string was previously displayed as "—" by the numeric formatter.
      const initial=Number(result.demo_start_cash??result.position?.demo_start_cash);
      result.budget_usd=Number.isFinite(initial)&&initial>0?initial:null;
      result.settlement=result.settlement||'USDC';
      result.strategy_mode=result.strategy_mode||'LONG';
      result.horizon=result.horizon||'15m';
    }
    return result;
  },[asset,getAccessToken,liveMode]);

  useEffect(()=>{
    const request=++activeRef.current;
    mutationRef.current++;
    setData(null);setBusy(false);setLoading(true);setFailed(false);
    if(!enabled||!getAccessToken){
      setLoading(false);
      return;
    }
    const controller=new AbortController();
    fetchOne(controller.signal).then(result=>{
      if(request===activeRef.current)setData(result);
    }).catch(()=>{
      if(request===activeRef.current)setFailed(true);
    }).finally(()=>{
      if(request===activeRef.current)setLoading(false);
    });
    return()=>{activeRef.current++;controller.abort();};
  },[asset,enabled,getAccessToken,fetchOne]);

  // Only a selected, already ACTIVE (or pending-exit) account is polled.
  // No market API calls, no app-driven trading and no timer for other coins.
  useEffect(()=>{
    if(!enabled||!data||!(['ACTIVE','EXIT_PENDING'].includes(data.status)||data.pause_exit_pending))return;
    let alive=true;
    let inFlight=false;
    const poll=async()=>{
      if(!alive||inFlight||busyRef.current||document.hidden)return;
      inFlight=true;
      const version=mutationRef.current;
      try{
        const result=await fetchOne();
        if(alive&&!busyRef.current&&version===mutationRef.current)
          setData(result);
      }catch{
        if(alive&&!busyRef.current&&version===mutationRef.current)
          setFailed(true);
      }finally{inFlight=false;}
    };
    const timer=setInterval(poll,20000);
    return()=>{alive=false;clearInterval(timer);};
  },[asset,enabled,data?.status,data?.pause_exit_pending,fetchOne]);

  const command=async action=>{
    // LIVE is controlled by the original Privy-signed per-coin session.
    // The paper start/resume API can never authorize a real transaction.
    if(liveMode&&!['pause','exit'].includes(action))return;
    if(action==='exit'&&(!liveMode||!['BTC','SOL'].includes(asset)||
       !['ACTIVE','PAUSED'].includes(data?.status)))return;
    if(busyRef.current||!enabled||!getAccessToken||!data||
       (action==='start' && (data.status!=='NOT_STARTED'||!extraPaperCanStart(options)))||
       (action==='pause' && !(liveMode?['ACTIVE','EXIT_PENDING'].includes(data.status):data.status==='ACTIVE'))||
       (action==='resume' && (data.status!=='PAUSED'||data.pause_exit_pending)))return;
    const request=activeRef.current;
    mutationRef.current++;
    busyRef.current=true;setBusy(true);setFailed(false);
    try{
      const token=await getAccessToken();
      if(!token)throw Error('Login required');
      const starting=action==='start';
      const payload=starting?{
        budget_usd:Number(options.budget),
        mode:options.mode,horizon:options.horizon,stable:options.stable,
      }:null;
      const endpoint=liveMode
        ? API_BASE+liveOwnerSessionPath(asset)+'/'+action
        : API_BASE+'/api/extra-paper/'+asset+'/'+action;
      const response=await fetch(endpoint,{
        method:'POST',cache:'no-store',
        headers:{Authorization:'Bearer '+token,Accept:'application/json',
                 ...(starting?{'Content-Type':'application/json'}:{})},
        ...(starting?{body:JSON.stringify(payload)}:{}),
      });
      if(!response.ok)throw Error(liveMode?'LIVE pause unavailable':'Paper command failed');
      const result=await response.json();
      if(liveMode){
        // A pause response does not establish the current owner session.
        // Re-read the authoritative chain-specific status after the command.
        const expected=action==='exit'?'EXIT_PENDING':'PAUSED';
        if(result?.asset!==asset||result?.status!==expected)
          throw Error('LIVE pause response does not match coin');
        const latest=await fetchOne();
        if(latest.status!==expected&&!(action==='exit'&&latest.status==='STOPPED'))
          throw Error('LIVE session state not confirmed');
        if(request===activeRef.current)setData(latest);
      }else{
        if(!['BTC','ETH'].includes(asset) && !validExtraPaperState(result,asset))
          throw Error('Invalid paper response');
        if(request===activeRef.current)setData(result);
      }
    }catch{
      if(request===activeRef.current)setFailed(true);
    }finally{
      busyRef.current=false;
      if(request===activeRef.current)setBusy(false);
    }
  };

  const state=data?.status;
  const liveCounters=liveMode?liveOwnerCounters(data,asset):null;
  const position=data?.position||{};
  const valuation=position.valuation||{};
  const cash=Number(position.stable_cash??0);
  const coinQty=Number(position.coin_quantity??0);
  const realized=Number(position.realized_net_profit??0);
  const fmt=n=>Number.isFinite(n)?n.toFixed(2):'—';
  const canStart=extraPaperCanStart(options);
  const runtime=data?.runtime_active_seconds;
  const hours=Number.isSafeInteger(runtime)?Math.floor(runtime/3600):0;
  const mins=Number.isSafeInteger(runtime)?Math.floor(runtime%3600/60):0;

  if(!enabled||!getAccessToken)return null;
  return <section className="mininex-extra-paper" aria-label={asset+' '+t.extraPaperTitle}>
    <div className="mininex-extra-paper-title">
      <strong>{liveMode?t.extraPaperTitleLive:t.extraPaperTitle}</strong>
      <small>{state==='ACTIVE'?t.extraPaperActive:
        state==='PAUSED'?t.extraPaperPaused:
        state==='STOPPED'?(document.documentElement.lang==='de'?'Gestoppt':'Stopped'):
        state==='EXIT_PENDING'?(document.documentElement.lang==='de'?'Ausstieg läuft':'Exit pending'):
        state==='NOT_STARTED'?t.extraPaperIdle:(liveMode?t.extraPaperLoadingLive:t.extraPaperLoading)}</small>
    </div>
    {liveMode&&data&&liveCounters&&<>
      <p className="privy-wallet-description">
        {t.extraPaperTitleLive}: {state==='ACTIVE'?t.extraPaperActive:
          state==='PAUSED'?t.extraPaperPaused:
          state==='EXIT_PENDING'?(document.documentElement.lang==='de'?'Ausstieg läuft':'Exit pending'):
          state==='STOPPED'?(document.documentElement.lang==='de'?'Gestoppt':'Stopped'):t.extraPaperIdle}
        {' · '}{data.worker_attached?'Worker: ACTIVE':'Worker: INACTIVE'}
      </p>
      <p className="privy-wallet-description" role="status">
        {t.extraPaperHistory}: {liveCounters.sent} / {liveCounters.finalized}
        {' '}(sent / finalized)
      </p>
      {['BTC','SOL'].includes(asset)&&['ACTIVE','PAUSED'].includes(state)&&
        <button type="button" className="privy-wallet-action" disabled={busy}
          onClick={()=>command('exit')}>Stop / Exit · {asset}</button>}
      {['ACTIVE','EXIT_PENDING'].includes(state)&&<button type="button" className="privy-wallet-action"
        disabled={busy} onClick={()=>command('pause')}>
        {t.extraPaperPause} · {asset}
      </button>}
    </>}
    {!liveMode&&loading&&<p className="privy-wallet-description">{t.extraPaperLoading}</p>}
    {failed&&<p className="privy-wallet-description" role="alert">
      {liveMode?'Live-Session noch nicht gestartet oder nicht verfügbar.':t.extraPaperUnavailable}
    </p>}
    {!liveMode&&(state==='NOT_STARTED'||failed||!data)&&<button type="button" className="privy-wallet-action" disabled={busy||!canStart}
        onClick={()=>command('start')}>
        {t.extraPaperStart} · {asset}
      </button>}
    {!liveMode&&['ACTIVE','PAUSED'].includes(state)&&<>
      <div className="mininex-extra-paper-balances">
        <div><span>{t.extraPaperBudget}</span><strong>{formatMoney(data.budget_usd,document.documentElement.lang)}</strong></div>
        <div><span>{t.extraPaperCash}</span>
          <strong>{fmt(cash)} {data.settlement}</strong></div>
        <div><span>{t.extraPaperHolding}</span>
          <strong>{coinQty.toPrecision(6)} {asset}</strong></div>
        <div><span>{t.extraPaperRealized}</span>
          <strong>{formatDisplaySigned(realized,document.documentElement.lang)}</strong></div>
        <div><span>{t.extraPaperEquity}</span>
          <strong>{valuation.quote_valid?formatMoney(valuation.estimated_total_equity_usd,document.documentElement.lang):'—'}</strong></div>
        <div><span>{t.extraPaperRuntime}</span>
          <strong>{hours}h {mins}m</strong></div>
      </div>
      
      {data.history_coverage&&<p className="privy-wallet-description"
        role="status">
        {t.extraPaperHistoryCoverage}: {
          data.history_coverage.available_minutes===null?'—':
          data.horizon==='24h'?
            (data.history_coverage.available_minutes/60).toFixed(1)+'h':
            data.history_coverage.available_minutes.toFixed(0)+'m'
        } / {data.horizon==='24h'?'24h':
          data.history_coverage.required_minutes+'m'}
        {data.history_coverage.complete?'': ' · '+t.extraPaperHistoryPartial}
      </p>}
      {state==='ACTIVE'&&<button type="button" className="privy-wallet-action"
        disabled={busy} onClick={()=>command('pause')}>
        {t.extraPaperPause} · {data.settlement}
      </button>}
      {state==='PAUSED'&&!data.pause_exit_pending&&
        <button type="button" className="privy-wallet-action" disabled={busy}
          onClick={()=>command('resume')}>{t.extraPaperResume} · {asset}</button>}
      {state==='PAUSED'&&data.pause_exit_pending&&
        <p role="status" className="privy-wallet-description">
          {t.extraPaperPending}
        </p>}
      <div className="mininex-extra-paper-history">
        <strong>{t.extraPaperHistory}</strong>
        {(data.trades||[]).slice(-4).reverse().map((trade,index)=>
          <div key={trade.at+'-'+index}>
            <span>{trade.kind==='PAPER_BUY'?t.extraReplayBuy:t.extraReplaySell}</span>
            <strong>{formatMoney(trade.total_spent??trade.net_proceeds??0,document.documentElement.lang)}</strong>
          </div>)}
        {data.trades.length===0&&
          <p className="privy-wallet-description">{t.extraPaperNoTrades}</p>}
      </div>
    </>}
    
  </section>;
}
