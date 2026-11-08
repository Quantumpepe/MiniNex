import React, {useCallback,useEffect,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE,formatTimestamp} from './data.js';
import {PRIVY_APP_ID} from './privyConfig.js';

const ASSETS=['BTC','ETH'];
const empty={status:'NOT_STARTED',paper_only:true,trades:[]};

function PersonalDemo({t}) {
  const {ready,authenticated,getAccessToken}=usePrivy();
  const [asset,setAsset]=useState('BTC');
  const [data,setData]=useState({BTC:empty,ETH:empty});
  const [status,setStatus]=useState('idle');
  const [busy,setBusy]=useState(false);
  const [paperVisible,setPaperVisible]=useState(false);
  useEffect(()=>{
    let active=true;
    fetch(API_BASE+'/api/user-paper/availability',{cache:'no-store'})
      .then(r=>{if(!r.ok)throw new Error('Unavailable');return r.json();})
      .then(data=>{if(active)setPaperVisible(data?.paper_available===true&&data?.live_trading_enabled===false);})
      .catch(()=>{if(active)setPaperVisible(false);});
    return()=>{active=false;};
  },[]);

  const fetchOne=useCallback(async current=>{
    const token=await getAccessToken();
    if(!token)throw new Error('Authentication required');
    const response=await fetch(API_BASE+'/api/user-paper/'+current,{
      headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
    });
    if(!response.ok)throw new Error('Session unavailable');
    const result=await response.json();
    if(result?.paper_only!==true||result?.real_trading_enabled!==false||
       result?.wallet_funds_used!==false)throw new Error('Invalid simulation');
    return result;
  },[getAccessToken]);

  useEffect(()=>{
    if(!ready||!authenticated||!paperVisible){setData({BTC:empty,ETH:empty});setStatus('idle');return;}
    let active=true;
    setStatus('loading');
    Promise.all(ASSETS.map(fetchOne)).then(rows=>{
      if(active){setData({BTC:rows[0],ETH:rows[1]});setStatus('ready');}
    }).catch(()=>{if(active)setStatus('failed');});
    return()=>{active=false;};
  },[ready,authenticated,fetchOne,paperVisible]);

  // Live UI refresh only; strategy execution remains fully server-side.
  useEffect(()=>{
    if(!ready||!authenticated||!paperVisible)return;
    let cancelled=false;
    let inFlight=false;
    const refresh=async()=>{
      if(inFlight||busy)return;
      inFlight=true;
      try{
        const rows=await Promise.all(ASSETS.map(fetchOne));
        if(!cancelled)setData({BTC:rows[0],ETH:rows[1]});
      }catch{
        // Keep the last proven simulation state during brief network errors.
      }finally{inFlight=false;}
    };
    const timer=setInterval(refresh,20000);
    return()=>{cancelled=true;clearInterval(timer);};
  },[ready,authenticated,fetchOne,busy,paperVisible]);

  const command=async action=>{
    if(busy||!ready||!authenticated)return;
    setBusy(true);
    try{
      const token=await getAccessToken();
      if(!token)throw new Error('Authentication required');
      const response=await fetch(API_BASE+'/api/user-paper/'+asset+'/'+action,{
        method:'POST',headers:{Authorization:'Bearer '+token,Accept:'application/json'},
        cache:'no-store'
      });
      if(!response.ok)throw new Error('Command failed');
      const result=await response.json();
      if(result?.asset!==asset||result?.paper_only!==true||
         result?.wallet_funds_used!==false||result?.real_trading_enabled!==false)
        throw new Error('Invalid response');
      setData(previous=>({...previous,[asset]:result}));
      setStatus('ready');
    }catch{setStatus('failed');}
    finally{setBusy(false);}
  };
  const current=data[asset]||empty;
  const position=current.position||{};
  if(!paperVisible)return null;
  return <section className="privy-hwm-box personal-paper" aria-label={t.personalDemoTitle}>
    <div className="privy-hwm-title"><strong>{t.personalDemoTitle}</strong></div>
    <p className="privy-wallet-description">{t.personalDemoNote}</p>
    <div className="personal-paper-tabs">
      {ASSETS.map(coin=><button key={coin} type="button" aria-pressed={asset===coin}
        className={asset===coin?'personal-paper-selected':''}
        onClick={()=>setAsset(coin)}>{coin}</button>)}
    </div>
    {!ready||!authenticated?<p className="privy-wallet-description">{t.taxLoginRequired}</p>:<>
      {status==='loading'&&<p className="privy-wallet-description">{t.privyBudgetLoading}</p>}
      {status==='failed'&&<p className="privy-wallet-description" role="alert">{t.personalDemoFailed}</p>}
      <p className="privy-wallet-description">
        {t.personalDemoStatus}: {current.status||'NOT_STARTED'} · {current.settlement||'USDC'} · {current.strategy_mode||'LONG'} · {current.horizon||'15m'}
      </p>
      {current.status!=='NOT_STARTED'&&<p className="privy-wallet-description">
        {t.personalDemoLastCheck}: {position.last_evaluated_at?
          formatTimestamp(position.last_evaluated_at,undefined,{year:'numeric',month:'2-digit',day:'2-digit'}):'—'} · {position.last_reason||'WAIT'}
      </p>}
      {current.status!=='NOT_STARTED'&&position.last_signal_summary&&
        <p className="privy-wallet-description">
          {String(position.last_signal_summary)}
        </p>}
      {current.status!=='NOT_STARTED'&&<div className="balance-grid">
        <div className="balance-line"><span>{t.personalDemoCash}</span><b>{position.stable_cash||'0'} {current.settlement}</b></div>
        <div className="balance-line"><span>{t.personalDemoCoin}</span><b>{position.coin_quantity||'0'} {asset}</b></div>
        <div className="balance-line"><span>{t.personalDemoPnl}</span><b>{position.realized_net_profit||'0'}</b></div>
        {position.valuation?.quote_valid===true&&Number(position.coin_quantity)>0&&<>
          <div className="balance-line">
            <span>{t.personalDemoExitNet}</span>
            <b>{Number(position.valuation.estimated_exit_net_usd).toFixed(2)} {current.settlement}</b>
          </div>
          {position.valuation.last_buy_costs_usd!=null&&
            <div className="balance-line">
              <span>{t.personalDemoBuyCosts}</span>
              <b>{Number(position.valuation.last_buy_costs_usd).toFixed(2)} {current.settlement}</b>
            </div>}
          <div className="balance-line">
            <span>{t.personalDemoSaleCosts}</span>
            <b>{Number(position.valuation.estimated_sale_costs_usd).toFixed(2)} {current.settlement}</b>
          </div>
          <p className="privy-wallet-description">
            {t.personalDemoSaleDetails}: {Number(position.valuation.estimated_sale_fee_usd).toFixed(2)}
            {' / '}{Number(position.valuation.estimated_sale_spread_slippage_usd).toFixed(2)}
            {' / '}{Number(position.valuation.estimated_sale_network_fee_usd).toFixed(2)} {current.settlement}
          </p>
          <div className="balance-line">
            <span>{t.personalDemoUnrealized}</span>
            <b>{Number(position.valuation.unrealized_net_pnl_usd).toFixed(2)} {current.settlement}
              {' ('+Number(position.valuation.unrealized_net_pct).toFixed(2)+' %)'}
            </b>
          </div>
        </>}
        <div className="balance-line"><span>{t.personalDemoTrades}</span><b>{position.trade_count||0}</b></div>
      </div>}
      <div className="personal-paper-controls">
        {current.status==='NOT_STARTED'&&<button type="button" disabled={busy||status==='loading'}
          onClick={()=>command('start')}>{t.personalDemoStart}</button>}
        {current.status==='ACTIVE'&&<>
          <button type="button" disabled={busy} onClick={()=>command('pause')}>{t.personalDemoPause}</button>
        </>}
        {current.status==='PAUSED'&&<button type="button" disabled={busy}
          onClick={()=>command('resume')}>{t.personalDemoResume}</button>}
        {status==='failed'&&<button type="button" disabled={busy}
          onClick={async()=>{
            setBusy(true);
            try{const result=await fetchOne(asset);setData(old=>({...old,[asset]:result}));setStatus('ready');}
            catch{setStatus('failed');}finally{setBusy(false);}
          }}>{t.privyBudgetReload}</button>}
      </div>
      {!!current.trades?.length&&<div className="tax-activity-list">
        {current.trades.slice(-8).reverse().map((trade,i)=><div className="tax-activity-row"
          key={trade.at+'-'+trade.kind+'-'+i}>
          <div><strong>{trade.kind?.replace('PAPER_','')||'TRADE'}</strong>
            <small>{formatTimestamp(trade.at,undefined,{year:'numeric',month:'2-digit',day:'2-digit'})}</small></div>
          <div><strong>{trade.quantity} {asset}</strong><small>{t.simulatedTradeLabel}</small></div>
        </div>)}
      </div>}
    </>}
  </section>;
}

export default function UserPaperSessions({t}) {
  return PRIVY_APP_ID?<PersonalDemo t={t}/>:null;
}
