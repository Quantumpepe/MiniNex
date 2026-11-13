import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {EXTRA_REPLAY_COINS,MODES,HORIZONS,validExtraReplayBudget,
  validExtraReplayResult} from './extraReplayRules.js';

// A single explicit historical simulation run; no wallet/private credentials,
// auto-started user paper sessions, orders, live quotes or background polling.
// Individual virtual budgets are held by the parent per coin for this UI visit.
export default function ExtraCoinStrategyReplay({asset,t,enabled,options,onChange}) {
  const [result,setResult]=useState(null);
  const [busy,setBusy]=useState(false);
  const [failed,setFailed]=useState(false);
  const ref=useRef({id:0,controller:null});
  const budgetOk=validExtraReplayBudget(options?.budget);

  useEffect(()=>{
    ref.current.id++;
    ref.current.controller?.abort();
    ref.current.controller=null;
    setResult(null);
    setFailed(false);
    setBusy(false);
    return()=>{ref.current.id++;ref.current.controller?.abort();};
  },[asset,enabled,options?.mode,options?.horizon,options?.budget]);

  const run=async(event)=>{
    event.preventDefault();
    if(!enabled||busy||!EXTRA_REPLAY_COINS.includes(asset)||
       !budgetOk||!MODES.includes(options?.mode)||
       !HORIZONS.includes(options?.horizon))return;
    const id=++ref.current.id;
    ref.current.controller?.abort();
    const controller=new AbortController();
    ref.current.controller=controller;
    const timeout=setTimeout(()=>controller.abort(),15000);
    setBusy(true);setFailed(false);setResult(null);
    try {
      const params=new URLSearchParams({
        mode:options.mode,horizon:options.horizon,initial_usd:options.budget,
      });
      const response=await fetch(API_BASE+'/api/strategy/replay/'+asset+'?'+params,{
        method:'GET',cache:'no-store',signal:controller.signal,
        headers:{Accept:'application/json'},
      });
      if(!response.ok)throw Error('Replay unavailable');
      const data=await response.json();
      if(!validExtraReplayResult(data,asset,options))throw Error('Invalid paper result');
      if(id===ref.current.id)setResult(data);
    }catch{
      if(id===ref.current.id)setFailed(true);
    }finally{
      clearTimeout(timeout);
      if(ref.current.controller===controller)ref.current.controller=null;
      if(id===ref.current.id)setBusy(false);
    }
  };

  const pct=value=>(value>0?'+':'')+value.toFixed(2)+'%';
  return <section className="mininex-extra-replay" aria-label={asset+' '+t.extraReplayTitle}>
    <strong>{t.extraReplayTitle}</strong>
    <p className="privy-wallet-description">{t.extraReplayExplanation}</p>
    <form className="mininex-extra-replay-form" onSubmit={run}>
      <label><span>{t.extraReplayBudget}</span>
        <input type="number" inputMode="numeric" min="100" max="5000" step="50"
          value={options.budget} disabled={!enabled||busy}
          onChange={event=>onChange(asset,'budget',event.target.value)}/>
      </label>
      <label><span>{t.privySetupMode}</span>
        <select value={options.mode} disabled={!enabled||busy}
          onChange={event=>onChange(asset,'mode',event.target.value)}>
          <option value="LONG">{t.privySetupLong}</option>
          <option value="SPOT_SHORT">{t.privySetupSpotShort}</option>
        </select>
      </label>
      <label><span>{t.privySetupHorizon}</span>
        <select value={options.horizon} disabled={!enabled||busy}
          onChange={event=>onChange(asset,'horizon',event.target.value)}>
          {HORIZONS.map(value=><option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <button type="submit" className="privy-wallet-action"
        disabled={!enabled||busy||!budgetOk}>
        {busy?t.extraReplayRunning:t.extraReplayRun}
      </button>
    </form>
    {!budgetOk&&<p className="privy-wallet-description" role="alert">
      {t.extraReplayBudgetInvalid}
    </p>}
    {failed&&<p className="privy-wallet-description" role="alert">
      {t.extraReplayUnavailable}
    </p>}
    {result&&result.status!=='SIMULATED'&&
      <p className="privy-wallet-description" role="status">
        {t.extraReplayInsufficient} ({result.sample_count} {t.extraReplaySamples})
      </p>}
    {result?.status==='SIMULATED'&&<div className="mininex-extra-replay-result" role="status">
      <div><span>{t.extraReplayNet}</span>
        <strong>{pct(result.estimated_net_return_pct)}</strong></div>
      <div><span>{t.extraReplayHold}</span>
        <strong>{pct(result.estimated_buy_and_hold_return_pct)}</strong></div>
      <div><span>{t.extraReplayVsHold}</span>
        <strong>{pct(result.estimated_strategy_minus_hold_pp)}</strong></div>
      <div><span>{t.extraReplayDrawdown}</span>
        <strong>{result.estimated_max_drawdown_pct.toFixed(2)}%</strong></div>
      <div><span>{t.extraReplayTrades}</span>
        <strong>{result.trade_count}</strong></div>
      <div><span>{t.extraReplayObserved}</span>
        <strong>{result.observed_minutes.toFixed(0)} min</strong></div>
      {result.trades.length>0&&<div className="mininex-extra-replay-trades">
        {result.trades.slice(-4).reverse().map((trade,index)=>
          <div key={trade.at+'-'+index}>
            <span>{trade.kind==='PAPER_BUY'?t.extraReplayBuy:t.extraReplaySell}</span>
            <strong>{Number(trade.total_spent??trade.net_proceeds??0).toFixed(2)} $</strong>
          </div>)}
      </div>}
      <p className="privy-wallet-description">{t.extraReplayCostWarning}</p>
    </div>}
  </section>;
}
