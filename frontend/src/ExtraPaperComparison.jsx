import React,{useEffect,useRef,useState} from 'react';
import {API_BASE} from './data.js';
import {validExtraPaperComparison} from './extraPaperComparisonRules.js';

// Load only after the user opens the panel. A single private GET replaces
// nine independent requests; no timers, trading controls or provider calls.
export default function ExtraPaperComparison({t,enabled,getAccessToken,userId}){
  const [expanded,setExpanded]=useState(false);
  const [busy,setBusy]=useState(false);
  const [data,setData]=useState(null);
  const [error,setError]=useState(false);
  const request=useRef({id:0,controller:null});

  useEffect(()=>{
    request.current.id++;
    request.current.controller?.abort();
    request.current.controller=null;
    setExpanded(false);setBusy(false);setData(null);setError(false);
    return()=>{
      request.current.id++;
      request.current.controller?.abort();
    };
  },[enabled,getAccessToken,userId]);

  const toggle=async()=>{
    const newId=++request.current.id;
    request.current.controller?.abort();
    request.current.controller=null;
    if(expanded){
      setExpanded(false);setData(null);setError(false);setBusy(false);
      return;
    }
    if(!enabled||!userId||!getAccessToken)return;
    setExpanded(true);setBusy(true);setError(false);setData(null);
    const controller=new AbortController();
    request.current.controller=controller;
    const timeout=setTimeout(()=>controller.abort(),12000);
    try{
      const token=await getAccessToken();
      if(!token)throw Error('Private comparison requires a login');
      const response=await fetch(API_BASE+'/api/extra-paper/comparison',{
        method:'GET',
        headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        cache:'no-store',signal:controller.signal,
      });
      if(!response.ok)throw Error('Comparison unavailable');
      const result=await response.json();
      if(!validExtraPaperComparison(result))throw Error('Invalid response');
      if(request.current.id===newId)setData(result);
    }catch{
      if(request.current.id===newId)setError(true);
    }finally{
      clearTimeout(timeout);
      if(request.current.controller===controller)request.current.controller=null;
      if(request.current.id===newId)setBusy(false);
    }
  };

  const money=value=>value===null||value===undefined?'—':
    (value>0?'+':'')+value.toFixed(2)+' $';
  const percent=value=>value===null||value===undefined?'—':
    (value>0?'+':'')+value.toFixed(2)+'%';
  const tone=value=>value>0?'positive':value<0?'negative':'neutral';

  return <section className="mininex-paper-compare">
    <button type="button" className="privy-wallet-action privy-wallet-action--secondary"
      disabled={!enabled||!getAccessToken||!userId}
      aria-expanded={expanded} onClick={toggle}>
      {t.extraCompareTitle} · 9
    </button>
    {expanded&&<div className="mininex-paper-compare-body">
      <p className="privy-wallet-description">{t.extraCompareHelp}</p>
      {busy&&<p role="status" className="privy-wallet-description">
        {t.extraCompareLoading}
      </p>}
      {error&&<p role="alert" className="privy-wallet-description">
        {t.extraCompareUnavailable}
      </p>}
      {data&&<div className="mininex-paper-compare-list">
        {data.assets.map(row=><div className="mininex-paper-compare-coin"
          key={row.asset}>
          <div className="mininex-paper-compare-head">
            <strong>{row.asset}</strong>
            <small>{row.status==='ACTIVE'?t.extraPaperActive:
              row.status==='PAUSED'?t.extraPaperPaused:
              row.status==='NOT_STARTED'?t.extraPaperIdle:
              t.extraCompareUnavailableShort}</small>
          </div>
          {row.status!=='NOT_STARTED'&&row.status!=='UNAVAILABLE'&&<>
            <div className="mininex-paper-compare-values">
              <div><span>{t.extraCompareNet}</span>
                <strong className={'mininex-pnl-'+tone(row.estimated_net_return_pct)}>
                  {percent(row.estimated_net_return_pct)}
                </strong></div>
              <div><span>{t.extraCompareRealized}</span>
                <strong className={'mininex-pnl-'+tone(row.cumulative_realized_pnl_usd)}>
                  {money(row.cumulative_realized_pnl_usd)}
                </strong></div>
              <div><span>{t.extraCompareTrades}</span>
                <strong>{row.trade_count}</strong></div>
              <div><span>{t.extraCompareSellWins}</span>
                <strong>{row.recent_winning_sells}/{row.recent_closed_sells}</strong></div>
              <div><span>{t.extraCompareCosts}</span>
                <strong>{row.recent_costs_complete?
                  money(row.recent_estimated_costs_usd):'—'}</strong></div>
            </div>
            {!row.quote_valid&&<small className="mininex-paper-compare-caution">
              {t.extraCompareNoQuote}
            </small>}
          </>}
        </div>)}
      </div>}
      {data&&<p className="privy-wallet-description">{t.extraCompareDisclaimer}</p>}
    </div>}
  </section>;
}
