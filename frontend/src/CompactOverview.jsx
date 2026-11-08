import React, {useMemo} from 'react';
import {Activity, ArrowRight, BarChart3, Bitcoin, Wallet} from 'lucide-react';
import {compactChartData} from './compact.js';
import {formatMoney,formatNumber,validMarket} from './data.js';

function AssetIcon({asset}) {
  return <span className={'compact-token compact-token--'+asset.toLowerCase()} aria-hidden="true">
    {asset==='BTC'?<Bitcoin size={21}/>:<span className="compact-eth">◆</span>}
  </span>;
}

function MarketChart({history,period,valid,t,asset}) {
  const chart=useMemo(()=>{
    const data=compactChartData(history,period==='h1'?3600:900);
    if(data.length<2)return null;
    const prices=data.map(p=>Number(p.price_usdt));
    const loRaw=Math.min(...prices),hiRaw=Math.max(...prices);
    const pad=Math.max(hiRaw-loRaw,Math.max(1,loRaw)*.0002)*.15;
    const lo=loRaw-pad,hi=hiRaw+pad;
    const start=data[0].timestamp,end=data.at(-1).timestamp;
    const x=p=>12+(p.timestamp-start)/Math.max(1,end-start)*276;
    const y=p=>90-(Number(p.price_usdt)-lo)/(hi-lo)*78;
    const line=data.map((p,i)=>(i?'L':'M')+x(p).toFixed(2)+' '+y(p).toFixed(2)).join(' ');
    return {line,area:line+' L288 96 L12 96 Z',last:y(data.at(-1)),color:prices.at(-1)>=prices[0]?'#5af3b4':'#ff9789'};
  },[history,period]);
  if(!valid||!chart)return <div className="compact-chart-empty"><BarChart3 size={24}/><span>{t.compactNoChart}</span></div>;
  return <svg className="compact-chart-large" viewBox="0 0 300 110" preserveAspectRatio="none" role="img" aria-label={asset+' '+t.compactChartLabel}>
    <defs><linearGradient id="compact-market-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={chart.color} stopOpacity=".28"/><stop offset="100%" stopColor={chart.color} stopOpacity="0"/></linearGradient></defs>
    {[30,60,90].map(y=><line key={y} x1="12" x2="288" y1={y} y2={y} stroke="#28484b" strokeWidth=".5"/>)}
    <path d={chart.area} fill="url(#compact-market-fill)"/>
    <path d={chart.line} fill="none" stroke={chart.color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
    <circle cx="288" cy={chart.last} r="4" fill={chart.color}/>
  </svg>;
}

export default function CompactOverview({t,lang,asset,setAsset,period,setPeriod,history,market,valid,marketSnapshot,goTo}) {
  const selected=valid?market:null;
  const change=selected?.change_24h_pct;
  const formatChange=value=>value==null||!Number.isFinite(Number(value))?'—':(Number(value)>0?'+':'')+formatNumber(value,lang)+'%';
  return <section className="compact-home compact-home--market" aria-label={t.overview}>
    <div className="compact-welcome"><h1>MiniNex</h1><span className="compact-home-label">{t.market}</span></div>
    <div className="compact-price-cards" role="tablist" aria-label={t.compactChooseAsset}>
      {['BTC','ETH'].map(a=>{
        const quote=marketSnapshot?.[a];
        const fresh=validMarket(quote);
        const movement=fresh?quote.change_24h_pct:null;
        return <button key={a} type="button" role="tab" aria-selected={asset===a}
          className={'compact-quote-card'+(asset===a?' active':'')}
          onClick={()=>{setAsset(a);setPeriod('m15');}}>
          <span className="compact-quote-heading"><AssetIcon asset={a}/><strong>{a==='BTC'?'Bitcoin':'Ethereum'}</strong><small>{a}</small></span>
          <strong className="compact-quote-price">{formatMoney(fresh?quote.price_usdt:null,lang)}</strong>
          <span className={'compact-quote-change '+(movement!=null?(Number(movement)>=0?'text-positive':'text-negative'):'')}>{formatChange(movement)} <small>24h</small></span>
        </button>;
      })}
    </div>
    <section className="compact-main-chart" aria-label={t.market}>
      <div className="compact-chart-top">
        <div className="compact-chart-title"><strong>{asset}/USDT</strong><span className={'compact-data-dot '+(valid?'valid':'')}/></div>
        <div className="compact-period" aria-label={t.compactChartPeriod}>
          <button type="button" className={period==='m15'?'on':''} aria-pressed={period==='m15'} onClick={()=>setPeriod('m15')}>15m</button>
          <button type="button" className={period==='h1'?'on':''} aria-pressed={period==='h1'} onClick={()=>setPeriod('h1')}>1h</button>
        </div>
      </div>
      <div className="compact-chart-price"><strong>{formatMoney(selected?.price_usdt,lang)}</strong><span className={change!=null?(Number(change)>=0?'text-positive':'text-negative'):''}>{formatChange(change)} <small>24h</small></span></div>
      <MarketChart history={history} period={period} valid={valid} t={t} asset={asset}/>
      <div className="compact-chart-footer">
        <span>{t.reference}</span><b>{formatMoney(valid?selected?.reference_price_usd:null,lang)}</b>
        <span>{t.spreadCheck}</span><b>{valid&&selected?.price_divergence_pct!=null?formatNumber(selected.price_divergence_pct,lang,3)+'%':'—'}</b>
      </div>
    </section>
    <div className="compact-actions">
      <button type="button" onClick={()=>goTo('activity')}><Activity size={18}/>{t.activity}<ArrowRight size={15}/></button>
      <button type="button" onClick={()=>goTo('wallet')}><Wallet size={18}/>{t.wallet}</button>
    </div>
  </section>;
}
