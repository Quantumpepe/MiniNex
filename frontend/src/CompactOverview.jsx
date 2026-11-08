import React, {useMemo} from 'react';
import {Activity, ArrowRight, BarChart3, Bitcoin, CheckCircle2, Coins, ShieldCheck, TrendingUp, Wallet, Zap} from 'lucide-react';
import {compactChartData, compactDecision, compactNetPnL} from './compact.js';
import {formatCoin, formatMoney, formatNumber, formatSigned, portfolioValue} from './data.js';

function TokenIcon({asset}) {
  return <span className={'compact-token compact-token--'+asset.toLowerCase()} aria-hidden="true">
    {asset==='BTC' ? <Bitcoin size={18}/> : <span className="compact-eth">◆</span>}
  </span>;
}
function PriceMini({history,trades,period,valid,t}) {
  const chart=useMemo(()=>{
    const data=compactChartData(history,period==='h1'?3600:900);
    if(data.length<2) return null;
    const prices=data.map(p=>Number(p.price_usdt));
    const min=Math.min(...prices),max=Math.max(...prices);
    const delta=Math.max(max-min,Math.max(1,min)*.0002);
    const lo=min-delta*.12,hi=max+delta*.12;
    const start=data[0].timestamp,end=data.at(-1).timestamp;
    const x=p=>12+(p.timestamp-start)/Math.max(1,end-start)*276;
    const y=p=>87-(Number(p.price_usdt)-lo)/(hi-lo)*72;
    const path=data.map((p,i)=>(i?'L':'M')+x(p).toFixed(2)+' '+y(p).toFixed(2)).join(' ');
    const spots=(Array.isArray(trades)?trades:[]).flatMap(trade=>{
      const at=Date.parse(trade.at||'')/1000;
      if(!Number.isFinite(at)||at<start||at>end) return [];
      return [{x:12+(at-start)/Math.max(1,end-start)*276,y:y({price_usdt:trade.price_reference||prices.at(-1)}),buy:trade.kind==='PAPER_BUY',key:String(at)+trade.kind}];
    });
    return {path,area:path+' L288 90 L12 90 Z',last:y(data.at(-1)),spots,color:prices.at(-1)>=prices[0]?'#5af3b4':'#ff9789'};
  },[history,trades,period]);
  if(!valid || !chart) return <div className="compact-spark-empty"><BarChart3 size={18}/><span>{t.compactNoChart}</span></div>;
  return <svg className="compact-sparkline" viewBox="0 0 300 100" preserveAspectRatio="none" aria-label={t.compactChartLabel} role="img">
    <defs><linearGradient id="compact-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={chart.color} stopOpacity=".23"/><stop offset="100%" stopColor={chart.color} stopOpacity="0"/></linearGradient></defs>
    <path d={chart.area} fill="url(#compact-fill)"/>
    <path d={chart.path} fill="none" stroke={chart.color} strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round"/>
    {chart.spots.map(p=><circle key={p.key} cx={p.x} cy={p.y} r="5.3" fill={p.buy?'#56e9b4':'#ffc17d'} stroke="#07191b" strokeWidth="2"/>)}
    <circle cx="288" cy={chart.last} r="4.3" fill={chart.color}/>
  </svg>;
}
function SmallMetric({label,value,icon:Icon,details,positive}) {
  return <div className="compact-stat">
    <div className="compact-stat-heading"><Icon size={15} strokeWidth={2.2}/><span>{label}</span></div>
    <div className={'compact-stat-value '+(positive===true?'text-positive':positive===false?'text-negative':'')}>{value}</div>
    {details?<div className="compact-stat-detail">{details}</div>:null}
  </div>;
}
export default function CompactOverview({t,lang,asset,setAsset,period,setPeriod,history,trades,market,valid,portfolio,paper,workerOnline,goTo}) {
  const price=valid?market?.price_usdt:null;
  const change=valid?market?.change_24h_pct:null;
  const value=portfolioValue(portfolio);
  const net=compactNetPnL(portfolio);
  const signal=compactDecision(portfolio?.last_decision,lang);
  const settlement=portfolio?.settlement||'USDT';
  const changeText=change==null?'—':(Number(change)>0?'+':'')+formatNumber(change,lang)+' %';
  return <section className="compact-home" aria-label={t.overview}>
    <div className="compact-welcome">
      <div><h1>MiniNex</h1></div>

    </div>
    <div className="compact-asset-switch" role="tablist" aria-label={t.compactChooseAsset}>
      {['BTC','ETH'].map(a=><button key={a} type="button" role="tab" aria-selected={asset===a} className={asset===a?'active':''} onClick={()=>{setAsset(a);setPeriod('m15');}}>
        <TokenIcon asset={a}/><span>{a==='BTC'?'Bitcoin':'Ethereum'}</span><small>{a}</small>
      </button>)}
    </div>
    <section className="compact-market" aria-label={t.market}>
      <div className="compact-market-primary">
        <div className="compact-market-text">
          <span className="compact-pair">{asset} / USDT <span className={'compact-data-dot '+(valid?'valid':'')} title={valid?t.dataVerified:t.dataUnverified}/></span>
          <strong className="compact-price">{formatMoney(price,lang)}</strong>
          <div className={'compact-variation '+(change!=null?(change>=0?'text-positive':'text-negative'):'')}>{changeText} <span>24h</span></div>
          <div className="compact-period" aria-label={t.compactChartPeriod}>
            <button type="button" className={period==='m15'?'on':''} aria-pressed={period==='m15'} onClick={()=>setPeriod('m15')}>15m</button>
            <button type="button" className={period==='h1'?'on':''} aria-pressed={period==='h1'} onClick={()=>setPeriod('h1')}>1h</button>
          </div>
        </div>
        <PriceMini history={history} trades={trades} period={period} valid={valid} t={t}/>
      </div>
    </section>
    <div className="compact-actions">

      <button type="button" onClick={()=>goTo('wallet')}><Wallet size={17}/>{t.wallet}</button>
    </div>
  </section>;
}
