import React,{useCallback,useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {validExtraPaperState,extraPaperCanStart} from './extraPaperRules.js';

// One opt-in server-side PAPER session per coin and account. Its simulation
// persists when Android closes; no wallet, live orders, approvals or deposits.
export default function ExtraCoinPaperControl({asset,t,enabled,getAccessToken,
                                               options,onChange}){
  const [data,setData]=useState(null);
  const [busy,setBusy]=useState(false);
  const [failed,setFailed]=useState(false);
  const [loading,setLoading]=useState(true);
  const activeRef=useRef(0);
  const busyRef=useRef(false);

  const fetchOne=useCallback(async(signal)=>{
    const token=await getAccessToken();
    if(!token)throw Error('Login required');
    const response=await fetch(API_BASE+'/api/extra-paper/'+asset,{
      method:'GET',
      headers:{Authorization:'Bearer '+token,Accept:'application/json'},
      cache:'no-store',signal,
    });
    if(!response.ok)throw Error('Personal paper state unavailable');
    const result=await response.json();
    if(!validExtraPaperState(result,asset))throw Error('Invalid paper session');
    return result;
  },[asset,getAccessToken]);

  useEffect(()=>{
    const request=++activeRef.current;
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
    if(!enabled||!data||!(data.status==='ACTIVE'||data.pause_exit_pending))return;
    let alive=true;
    let inFlight=false;
    const poll=async()=>{
      if(!alive||inFlight||busyRef.current||document.hidden)return;
      inFlight=true;
      try{
        const result=await fetchOne();
        if(alive)setData(result);
      }catch{
        if(alive)setFailed(true);
      }finally{inFlight=false;}
    };
    const timer=setInterval(poll,20000);
    return()=>{alive=false;clearInterval(timer);};
  },[asset,enabled,data?.status,data?.pause_exit_pending,fetchOne]);

  const command=async action=>{
    if(busyRef.current||!enabled||!getAccessToken||!data||
       (action==='start' && (data.status!=='NOT_STARTED'||!extraPaperCanStart(options)))||
       (action==='pause' && data.status!=='ACTIVE')||
       (action==='resume' && (data.status!=='PAUSED'||data.pause_exit_pending)))return;
    const request=activeRef.current;
    busyRef.current=true;setBusy(true);setFailed(false);
    try{
      const token=await getAccessToken();
      if(!token)throw Error('Login required');
      const starting=action==='start';
      const payload=starting?{
        budget_usd:Number(options.budget),
        mode:options.mode,horizon:options.horizon,stable:options.stable,
      }:null;
      const response=await fetch(API_BASE+'/api/extra-paper/'+asset+'/'+action,{
        method:'POST',cache:'no-store',
        headers:{Authorization:'Bearer '+token,Accept:'application/json',
                 ...(starting?{'Content-Type':'application/json'}:{})},
        ...(starting?{body:JSON.stringify(payload)}:{}),
      });
      if(!response.ok)throw Error('Paper command failed');
      const result=await response.json();
      if(!validExtraPaperState(result,asset))throw Error('Invalid response');
      if(request===activeRef.current)setData(result);
    }catch{
      if(request===activeRef.current)setFailed(true);
    }finally{
      busyRef.current=false;
      if(request===activeRef.current)setBusy(false);
    }
  };

  const state=data?.status;
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
      <strong>{t.extraPaperTitle}</strong>
      <small>{state==='ACTIVE'?t.extraPaperActive:
        state==='PAUSED'?t.extraPaperPaused:
        state==='NOT_STARTED'?t.extraPaperIdle:t.extraPaperLoading}</small>
    </div>
    {loading&&<p className="privy-wallet-description">{t.extraPaperLoading}</p>}
    {failed&&<p className="privy-wallet-description" role="alert">
      {t.extraPaperUnavailable}
    </p>}
    {state==='NOT_STARTED'&&<>
      <label className="mininex-extra-paper-stable">
        <span>{t.privySetupStable}</span>
        <select disabled={busy} value={options?.stable||'USDC'}
          onChange={e=>onChange(asset,'stable',e.target.value)}>
          <option value="USDC">USDC</option><option value="USDT">USDT</option>
        </select>
      </label>
      <p className="privy-wallet-description">{t.extraPaperStartNote}</p>
      <button type="button" className="privy-wallet-action" disabled={busy||!canStart}
        onClick={()=>command('start')}>
        {t.extraPaperStart} · {asset}
      </button>
    </>}
    {['ACTIVE','PAUSED'].includes(state)&&<>
      <div className="mininex-extra-paper-balances">
        <div><span>{t.extraPaperBudget}</span><strong>{fmt(data.budget_usd)} $</strong></div>
        <div><span>{t.extraPaperCash}</span>
          <strong>{fmt(cash)} {data.settlement}</strong></div>
        <div><span>{t.extraPaperHolding}</span>
          <strong>{coinQty.toPrecision(6)} {asset}</strong></div>
        <div><span>{t.extraPaperRealized}</span>
          <strong>{realized>0?'+':''}{fmt(realized)} $</strong></div>
        <div><span>{t.extraPaperEquity}</span>
          <strong>{valuation.quote_valid?fmt(valuation.estimated_total_equity_usd)+' $':'—'}</strong></div>
        <div><span>{t.extraPaperRuntime}</span>
          <strong>{hours}h {mins}m</strong></div>
      </div>
      <p className="privy-wallet-description">{t.extraPaperRunningNote}</p>
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
            <strong>{fmt(Number(trade.total_spent??trade.net_proceeds??0))} $</strong>
          </div>)}
        {data.trades.length===0&&
          <p className="privy-wallet-description">{t.extraPaperNoTrades}</p>}
      </div>
    </>}
    <p className="privy-wallet-description">{t.extraPaperOnly}</p>
  </section>;
}
