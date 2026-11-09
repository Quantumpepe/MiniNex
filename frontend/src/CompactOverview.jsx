import React, {useMemo} from 'react';
import miniNexLogoData from '../branding/mininex-logo.webp.b64?raw';
import {Activity, ArrowDownLeft, ArrowRight, ArrowUpRight, BarChart3, Bitcoin, Settings2, Wallet} from 'lucide-react';
import {compactChartData} from './compact.js';
import {paperSparkline} from './paperPortfolio.js';
import {formatMoney,formatNumber,formatSigned,formatTimestamp,validMarket} from './data.js';

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


function PaperPortfolioCard({t,lang,summary,samples,authenticated,goTo,paperAvailable}) {
  const chart=useMemo(()=>paperSparkline(samples),[samples]);
  const positive=summary?.pnl>=0;
  const color=positive?'#5bf2b6':'#ff9388';
  const label=lang==='de'?'Portfolio (Gesamtwert)':'Portfolio (total value)';
  const chartLabel=lang==='de'?'Beobachteter Verlauf seit App-Start':'Observed since opening the app';
  const prompt=!paperAvailable
    ? (lang==='de'?'Keine verifizierten Kontowerte verfügbar':'No verified account values available')
    : authenticated
      ? (lang==='de'?'Simulation starten oder Kursdaten prüfen':'Start a simulation or check market quotes')
      : (lang==='de'?'Zum Anzeigen anmelden':'Sign in to view paper portfolio');
  return <section className="compact-wallet-portfolio" aria-label={label}>
    <div className="compact-wallet-top">
      <span>{label} · BTC + ETH</span>
      <button type="button" className="compact-wallet-open" onClick={()=>goTo('wallet')} aria-label={t.wallet} title={t.wallet}><Wallet size={16}/></button>
    </div>
    <div className="compact-wallet-body">
      <div className="compact-wallet-values">
        <strong>{summary?formatMoney(summary.equity,lang):'—'}</strong>
        {summary
          ? <span className={positive?'text-positive':'text-negative'}>
              {formatSigned(summary.pnl,lang)} ({summary.pnlPct>=0?'+':''}{formatNumber(summary.pnlPct,lang,2)}%)
            </span>
          : <small>{prompt}</small>}
      </div>
      <div className="compact-wallet-curve">
        {summary&&chart
          ? <svg viewBox="0 0 200 50" role="img" aria-label={chartLabel} preserveAspectRatio="none">
              <defs><linearGradient id="paper-equity-fill" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity=".24"/>
                <stop offset="100%" stopColor={color} stopOpacity="0"/>
              </linearGradient></defs>
              <path d={chart.area} fill="url(#paper-equity-fill)"/>
              <path d={chart.line} fill="none" stroke={chart.positive?'#5bf2b6':'#ff9388'} strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"/>
              <circle cx="192" cy={chart.lastY} r="3.5" fill={chart.positive?'#5bf2b6':'#ff9388'}/>
            </svg>
          : <span className="compact-wallet-no-curve">{lang==='de'?'Verlauf wird gesammelt':'Collecting portfolio history'}</span>}
      </div>
    </div>
    <div className="compact-wallet-foot">
      <span>{paperAvailable?'PAPER':'LIVE —'} · {paperAvailable ? (lang==='de'?'kein Echtgeld':'no real funds') : (lang==='de'?'nicht freigeschaltet':'not enabled')}</span>
      <span>{chart ? chartLabel : (lang==='de'?'Werte nach geschätzten Verkaufskosten':'After estimated exit costs')}</span>
    </div>
  </section>;
}

export default function CompactOverview({t,lang,chooseLang,asset,setAsset,period,setPeriod,history,market,valid,marketSnapshot,trades,goTo,showSimulatedTrades=true,portfolioSummary=null,portfolioSamples=[],portfolioAuthenticated=false}) {
  const selected=valid?market:null;
  const change=selected?.change_24h_pct;
  const formatChange=value=>value==null||!Number.isFinite(Number(value))?'—':(Number(value)>0?'+':'')+formatNumber(value,lang)+'%';
  return <section className="compact-home compact-home--market" aria-label={t.overview}>
    <div className="compact-welcome">
      <button className="compact-home-brand" type="button" onClick={()=>goTo('overview')} aria-label="MiniNex">
        <img src={`data:image/webp;base64,${miniNexLogoData.trim()}`} alt="" aria-hidden="true"/>
        <strong>mini<span>nex</span></strong>
      </button>
      <div className="compact-home-actions">
        <div className="lang-switch" role="group" aria-label={t.language}>
          <button type="button" className={lang==='de'?'selected':''} onClick={()=>chooseLang('de')} aria-pressed={lang==='de'}>DE</button>
          <button type="button" className={lang==='en'?'selected':''} onClick={()=>chooseLang('en')} aria-pressed={lang==='en'}>EN</button>
        </div>
        <button className="icon-btn compact-settings-btn" type="button" aria-label={t.settings} title={t.settings} onClick={()=>goTo('settings')}><Settings2 size={17}/></button>
      </div>
    </div>
    <PaperPortfolioCard t={t} lang={lang} summary={portfolioSummary} samples={portfolioSamples} authenticated={portfolioAuthenticated} paperAvailable={showSimulatedTrades} goTo={goTo}/>
    <div className="compact-market-modes" role="group" aria-label={t.compactMode}>
      <span className={'compact-market-mode'+(portfolioSummary?.runningAssets>0?' is-running':'')} title={lang==='de'?'Automatische Simulation':'Automatic paper simulation'}><Activity size={14}/>{t.compactAuto}<small>{portfolioSummary?.runningAssets>0?(lang==='de'?'Läuft':'Running'):(lang==='de'?'Inaktiv':'Inactive')}</small></span>
      <span className={'compact-market-mode'+(showSimulatedTrades?' is-selected':'')}><span className="compact-mode-dot"/>{'PAPER'}<small>{showSimulatedTrades?(lang==='de'?'Simulation':'Simulation'):(lang==='de'?'Aus':'Off')}</small></span>
      <span className="compact-market-mode is-locked" aria-disabled="true" title={lang==='de'?'Echtgeldhandel noch nicht freigeschaltet':'Live trading not yet enabled'}><Wallet size={13}/>{'LIVE'}<small>{lang==='de'?'Gesperrt':'Locked'}</small></span>
    </div>
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
    {showSimulatedTrades&&<section className="compact-recent-trades" aria-label={t.recentTrades}>
      <div className="compact-recent-heading"><strong>{t.recentTrades} · {asset}</strong><span>{t.simulatedTradeLabel}</span></div>
      {Array.isArray(trades)&&trades.length ? <div className="compact-trade-list">
        {[...trades].reverse().slice(0,4).map((trade,index)=>{
          const buy=trade.kind==='PAPER_BUY';
          const amount=buy?trade.total_spent:trade.net_proceeds;
          const pnl=!buy&&trade.realized_net_profit!=null?Number(trade.realized_net_profit):null;
          return <div className="compact-trade-row" key={(trade.at||index)+'-'+index}>
            <span className={'compact-trade-icon '+(buy?'is-buy':'is-sell')}>{buy?<ArrowDownLeft size={16}/>:<ArrowUpRight size={16}/>}</span>
            <div className="compact-trade-detail"><strong>{buy?t.buy:t.sell} · {asset}</strong><small>{formatTimestamp(trade.at,lang,{day:'2-digit',month:'short'})}</small></div>
            <div className="compact-trade-value"><strong>{formatMoney(amount,lang)}</strong>{pnl!=null&&<small className={pnl>=0?'text-positive':'text-negative'}>{formatSigned(pnl,lang)}</small>}</div>
          </div>;
        })}
      </div> : <p className="compact-trade-empty">{t.noRecentTrades}</p>}
    </section>}
    <div className="compact-actions">
      <button type="button" onClick={()=>goTo('activity')}><Activity size={18}/>{t.activity}<ArrowRight size={15}/></button>
      <button type="button" onClick={()=>goTo('wallet')}><Wallet size={18}/>{t.wallet}</button>
    </div>
  </section>;
}
