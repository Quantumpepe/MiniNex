import React, {useMemo} from 'react';
import miniNexLogoData from '../branding/mininex-logo.webp.b64?raw';
import {ArrowDownLeft, ArrowUpRight, BarChart3, Bitcoin, Settings2, Wallet} from 'lucide-react';
import {compactChartData} from './compact.js';
import {paperSparkline} from './paperPortfolio.js';
import {livePortfolioSparkline} from './livePortfolioHistory.js';
import {formatMoney,formatAssetMoney,formatAssetQuantity,formatNumber,formatSigned,formatTimestamp,validMarket} from './data.js';
import CoinGrid from './CoinGrid.jsx';
import HomeAccount from './HomeAccount.jsx';
import {coinBySymbol} from './coins.js';

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


function PaperPortfolioCard({t,lang,summary,samples,authenticated,goTo,scope,status,paperMode=true}) {
  const chart=useMemo(()=>paperMode
    ?paperSparkline(samples,{recentMs:15*60*1000,allowSingle:true})
    :livePortfolioSparkline(samples),[samples,paperMode]);
  const positive=summary?.pnl!=null&&summary.pnl>=0;
  // PAPER color is based on TOTAL cumulative PnL, not the first/last
  // short-window sample. LIVE wallet holdings have no verified cost basis,
  // so display their price movements neutrally rather than as profit/loss.
  const color=paperMode?(positive?'#5bf2b6':'#ff9388'):'#9ebfc0';
  const label='Portfolio';
  const chartLabel=paperMode
    ?(lang==='de'?'Beobachteter Gesamtportfolio-Verlauf':'Observed total portfolio equity')
    :(lang==='de'?'Beobachteter Wallet-Verlauf':'Observed wallet value history');
  const collecting=lang==='de'?'Verlauf wird erfasst':'Collecting history';
  const partial=!paperMode&&(summary?.incomplete===true||!!summary?.missingCoverage);
  return <section className="compact-wallet-portfolio" aria-label={label}>
    <div className="compact-wallet-top">
      <span>{label}</span>
      <button type="button" className="compact-wallet-open" onClick={()=>goTo('wallet')} aria-label={t.wallet} title={t.wallet}><Wallet size={16}/></button>
    </div>
    <div className="compact-wallet-body">
      <div className="compact-wallet-values">
        <strong>{summary?.valuationAvailable?formatMoney(summary.equity,lang):'—'}</strong>
        {paperMode&&summary?.valuationAvailable
          ? <span className={positive?'text-positive':'text-negative'}>
              {formatSigned(summary.pnl,lang)} ({summary.pnlPct>=0?'+':''}{formatNumber(summary.pnlPct,lang,2)}%)
            </span>
          : null}
        {!paperMode&&summary?.holdings?.length>0&&<small className="compact-wallet-holdings">
          {summary.holdings.map(h=>h.asset+' '+formatAssetQuantity(h.balance,h.asset,lang)).join(' · ')}
        </small>}
      </div>
      <div className="compact-wallet-curve">
        {authenticated&&chart
          ? <svg viewBox="0 0 200 50" role="img" aria-label={chartLabel} preserveAspectRatio="none">
              <defs><linearGradient id="paper-equity-fill" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity=".24"/>
                <stop offset="100%" stopColor={color} stopOpacity="0"/>
              </linearGradient></defs>
              {chart.area&&<path d={chart.area} fill="url(#paper-equity-fill)"/>}
              <path d={chart.line} fill="none" stroke={color} strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"/>
              <circle cx="192" cy={chart.lastY} r="3.5" fill={color}/>
            </svg>
          : summary?.valuationAvailable?<small className="compact-wallet-chart-empty">{collecting}</small>:null}
      </div>
    </div>
    <div className="compact-wallet-foot">
      <span>{paperMode?'PAPER':'LIVE'}</span>
      {!paperMode&&summary?.cached&&<small>{lang==='de'?'Letzter Stand':'Last known value'}</small>}
      {partial&&<small>{lang==='de'?'Teilwert':'Partial value'}</small>}
    </div>
  </section>;
}

export default function CompactOverview({t,lang,chooseLang,asset,setAsset,period,setPeriod,history,market,valid,marketSnapshot,syncedAt=0,trades,goTo,showSimulatedTrades=true,portfolioSummary=null,portfolioSamples=[],portfolioAuthenticated=false,portfolioStatus='ready',walletPortfolio=null,tradesStatus='ready',showAllTrades=false,onToggleAllTrades}) {
  const selected=valid?market:null;
  const change=selected?.change_24h_pct;
  const formatChange=value=>value==null||!Number.isFinite(Number(value))?'—':(Number(value)>0?'+':'')+formatNumber(value,lang)+'%';
  // Keep every verified account-bound PAPER trade supplied by the API.
  // Only the visible viewport is capped at four rows; the rest scrolls inside.
  const recentTrades=Array.isArray(trades)?trades.slice(0,60):[];
  const hasMoreTrades=recentTrades.length>4;
  return <section className="compact-home compact-home--market" aria-label={t.overview}>
    <div className="compact-welcome">
      <button className="compact-home-brand" type="button" onClick={()=>goTo('overview')} aria-label="MiniNex">
        <img src={`data:image/webp;base64,${miniNexLogoData.trim()}`} alt="" aria-hidden="true"/>
        <strong>mini<span>nex</span></strong>
      </button>
      <div className="compact-home-actions">
        <HomeAccount t={t}/>
        <div className="lang-switch" role="group" aria-label={t.language}>
          <button type="button" className={lang==='de'?'selected':''} onClick={()=>chooseLang('de')} aria-pressed={lang==='de'}>DE</button>
          <button type="button" className={lang==='en'?'selected':''} onClick={()=>chooseLang('en')} aria-pressed={lang==='en'}>EN</button>
        </div>
        <button className="icon-btn compact-settings-btn" type="button" aria-label={t.settings} title={t.settings} onClick={()=>goTo('settings')}><Settings2 size={17}/></button>
      </div>
    </div>
    <PaperPortfolioCard t={t} lang={lang}
      summary={showSimulatedTrades?portfolioSummary:walletPortfolio?.summary}
      samples={showSimulatedTrades?portfolioSamples:(walletPortfolio?.samples||[])}
      authenticated={showSimulatedTrades?portfolioAuthenticated:walletPortfolio?.authenticated}
      status={showSimulatedTrades?portfolioStatus:walletPortfolio?.status}
      paperMode={showSimulatedTrades}
      scope="ALL" goTo={goTo}/>
    <CoinGrid t={t} lang={lang} selected={asset}
      markets={marketSnapshot} syncedAt={syncedAt} kind="home"
      onSelect={symbol=>{
        if(coinBySymbol(symbol)){
          setAsset(symbol);setPeriod('m15');
        }
      }}/>
    <section className="compact-main-chart" aria-label={t.market}>
      <div className="compact-chart-top">
        <div className="compact-chart-title"><strong>{asset}/USDT</strong><span className={'compact-data-dot '+(valid?'valid':'')}/></div>
        <div className="compact-period" aria-label={t.compactChartPeriod}>
          <button type="button" className={period==='m15'?'on':''} aria-pressed={period==='m15'} onClick={()=>setPeriod('m15')}>15m</button>
          <button type="button" className={period==='h1'?'on':''} aria-pressed={period==='h1'} onClick={()=>setPeriod('h1')}>1h</button>
        </div>
      </div>
      <div className="compact-chart-price"><strong>{formatAssetMoney(selected?.price_usdt,asset,lang)}</strong><span className={change!=null?(Number(change)>=0?'text-positive':'text-negative'):''}>{formatChange(change)} <small>24h</small></span></div>
      <MarketChart history={history} period={period} valid={valid} t={t} asset={asset}/>
      <div className="compact-chart-footer">
        <span>{t.reference}</span><b>{formatAssetMoney(valid?selected?.reference_price_usd:null,asset,lang)}</b>
        <span>{t.spreadCheck}</span><b>{valid&&selected?.price_divergence_pct!=null?formatNumber(selected.price_divergence_pct,lang,3)+'%':'—'}</b>
      </div>
    </section>
    {!showSimulatedTrades&&!coinBySymbol(asset)?.paperSettingsReady&&<p className="compact-market-readonly">{lang==='de'?'Nur Kursanzeige · Handel in Vorbereitung':'Market data only · Trading coming later'}</p>}
    <section className="compact-recent-trades" aria-label={t.recentTrades}>
      <div className="compact-recent-heading">
        <strong>{t.recentTrades} · {showAllTrades?t.homeAllCoins:asset}
          <small className="home-history-paper-label">{showSimulatedTrades?t.simulatedTradeLabel:'LIVE'}</small>
        </strong>
        <button type="button" className="home-history-filter"
          aria-pressed={showAllTrades} onClick={onToggleAllTrades}>
          {showAllTrades?t.homeOnlyCoin:t.homeAllCoins}
        </button>
      </div>
      {tradesStatus==='loading'?<p className="compact-trade-empty">{t.homeTradesLoading}</p>
       :tradesStatus==='error'?<p className="compact-trade-empty" role="alert">{t.homeTradesUnavailable}</p>
       :!portfolioAuthenticated?<p className="compact-trade-empty">{t.homeTradesSignIn}</p>
       :recentTrades.length ? <div className={'compact-trade-list'+(hasMoreTrades?' compact-trade-list--scroll':'')}
        tabIndex={hasMoreTrades?0:undefined}
        role={hasMoreTrades?'region':undefined}
        aria-label={hasMoreTrades?t.recentTrades+' · '+(showAllTrades?t.homeAllCoins:asset):undefined}>
        {recentTrades.map((trade,index)=>{
          const buy=trade.kind==='PAPER_BUY'||trade.kind==='LIVE_BUY';
          const amount=trade.amount_usd;
          const pnl=!buy&&trade.realized_net_profit!=null?Number(trade.realized_net_profit):null;
          return <div className="compact-trade-row" key={trade.id}>
            <span className={'compact-trade-icon '+(buy?'is-buy':'is-sell')}>{buy?<ArrowDownLeft size={16}/>:<ArrowUpRight size={16}/>}</span>
            <div className="compact-trade-detail"><strong>{buy?t.buy:t.sell} · {trade.asset}</strong><small>{trade.price_reference!=null&&<>{t.homeTradePrice}: {formatAssetMoney(trade.price_reference,trade.asset,lang)} · </>}{formatTimestamp(trade.at,lang,{day:'2-digit',month:'short'})}</small></div>
            <div className="compact-trade-value"><strong>{trade.settlement_asset?formatAssetQuantity(trade.settlement_amount,trade.settlement_asset,lang)+' '+trade.settlement_asset:formatMoney(amount,lang)}</strong>{pnl!=null&&<small className={pnl>=0?'text-positive':'text-negative'}>{formatSigned(pnl,lang)}</small>}</div>
          </div>;
        })}
      </div> : <p className="compact-trade-empty">{t.noRecentTrades}</p>}
    </section>
  </section>;
}
