import miniNexLogoData from '../branding/mininex-logo.webp.b64?raw';
import {App as CapacitorApp} from '@capacitor/app';
import {Capacitor} from '@capacitor/core';
import {usePrivy} from '@privy-io/react-auth';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, ArrowDownLeft, ArrowRight, ArrowUpRight, BarChart3, Bitcoin,
  Check, ChevronDown, CircleAlert, Clock3, Globe2,
  History, Info, LayoutDashboard, LockKeyhole, RefreshCcw,
  Settings2, ShieldCheck, Sparkles, TrendingDown, TrendingUp, Wallet,
  Wifi, WifiOff, Zap,
} from 'lucide-react';
import {DICTIONARY, getLanguage} from './i18n.js';
import CompactOverview from './CompactOverview.jsx';
import {summarizePaperSessions} from './paperPortfolio.js';
import HelpCenter from './HelpCenter.jsx';
import ActivityReport from './ActivityReport.jsx';
import BlockchainPreview from './BlockchainPreview.jsx';
import PrivyWalletPanel from './PrivyWalletPanel.jsx';
import UserPaperSessions from './UserPaperSessions.jsx';
import UpdateStatus from './UpdateStatus.jsx';
import {
  API_BASE, ensurePaperOnly, formatCoin, formatMoney, formatNumber,
  formatRelative, formatSigned, formatTimestamp, limitHistory,
  portfolioValue, validMarket,
} from './data.js';

const ASSETS = ['BTC', 'ETH'];
const NAV = [
  {key:'overview', icon:LayoutDashboard},
  {key:'activity', icon:History},
  {key:'wallet', icon:Wallet},
  {key:'settings', icon:Settings2},
];
const WINDOW = {m15:900, h1:3600};
const FRESH_MS = 20000;

async function getJson(path) {
  const response = await fetch(`${API_BASE}${path}`, {headers:{Accept:'application/json'}, cache:'no-store'});
  if (!response.ok) throw new Error(`API ${response.status}: ${path}`);
  return response.json();
}

function MiniNexMark({size=38}) {
  return <div className="brand-mark" style={{width:size,height:size,borderRadius:'50%',background:'transparent',boxShadow:'none',overflow:'hidden',padding:0}} aria-hidden="true"><img src={`data:image/webp;base64,${miniNexLogoData.trim()}`} alt="" style={{width:'100%',height:'100%',objectFit:'cover',display:'block'}} /></div>;
}

function AssetSymbol({asset,size='md'}) {
  return <span className={`asset-symbol asset-symbol--${asset.toLowerCase()} asset-symbol--${size}`} aria-hidden="true">
    {asset==='BTC'? <Bitcoin size={size==='sm'?18:23} strokeWidth={2.6}/> : <span className="eth-diamond">◆</span>}
  </span>;
}

function Pill({children,tone='neutral',icon:Icon}) {
  return <span className={`pill pill--${tone}`}>{Icon?<Icon size={12} strokeWidth={2.6}/>:null}{children}</span>;
}

function Value({number,lang,suffix='$',prefix='',digits=2}) {
  const n=Number(number);
  if(number==null || !Number.isFinite(n)) return <span>—</span>;
  return <span>{prefix}{formatNumber(n,lang,digits)} {suffix}</span>;
}

function PriceChart({history,asset,period,market,trades,t,lang}) {
  const [hover,setHover] = useState(null);
  const svgRef = useRef(null);
  const recent = useMemo(()=>limitHistory(history, WINDOW[period]||900),[history,period]);
  const model = useMemo(()=>{
    if(!recent.length) return null;
    const n=recent.length;
    const prices=recent.map(p=>Number(p.price_usdt));
    const rawMin=Math.min(...prices), rawMax=Math.max(...prices);
    const pad=Math.max(rawMax-rawMin,rawMin*.0009)*.20;
    const lo=rawMin-pad, hi=rawMax+pad;
    const left=22, right=798, top=25, bottom=229;
    const start=recent[0].timestamp, end=recent[n-1].timestamp;
    const x=i=>left + ((recent[i].timestamp-start)/Math.max(1,end-start))*(right-left);
    const y=value=>bottom-((value-lo)/Math.max(.0001,hi-lo))*(bottom-top);
    const coords=recent.map((p,i)=>[x(i),y(Number(p.price_usdt))]);
    const line=coords.map(([cx,cy],i)=>`${i===0?'M':'L'}${cx.toFixed(2)} ${cy.toFixed(2)}`).join(' ');
    const area=`${line} L${coords[n-1][0].toFixed(2)} ${bottom} L${coords[0][0].toFixed(2)} ${bottom} Z`;
    const marks=(Array.isArray(trades)?trades:[]).map((trade,i)=>{
      const at=Date.parse(trade.at||'')/1000;
      if(!Number.isFinite(at)||at<start||at>end) return null;
      const cx=left+(at-start)/Math.max(1,end-start)*(right-left);
      return {trade,i,x:cx,y:y(Number(trade.price_reference)||prices.at(-1))};
    }).filter(Boolean);
    return {lo,hi,coords,area,line,marks,first:start,last:end,positive:prices.at(-1)>=prices[0]};
  },[recent,trades]);

  const onMove=event=>{
    if(!model||!svgRef.current) return;
    const rect=svgRef.current.getBoundingClientRect();
    const px=(event.clientX-rect.left)*820/Math.max(1,rect.width);
    let closest=0, distance=Infinity;
    for(let i=0;i<model.coords.length;i++) {
      const delta=Math.abs(model.coords[i][0]-px);
      if(delta<distance){closest=i;distance=delta;}
    }
    setHover(closest);
  };
  const selected = model && hover!=null && recent[hover] ? {point:recent[hover],coords:model.coords[hover]}:null;
  const last = model?.coords.at(-1);
  return <div className="chart-frame">
    <div className="chart-headerline">
      
      <span className="tiny-muted">{recent.length>0 ? `${formatTimestamp(recent[0].timestamp,lang)} – ${formatTimestamp(recent.at(-1).timestamp,lang)}` : '—'}</span>
    </div>
    {!model || recent.length<2 ? <div className="chart-empty"><BarChart3 size={33}/><strong>{t.noHistory}</strong></div>
    : <div className="chart-svg-wrap">
      <svg ref={svgRef} className="chart-svg" viewBox="0 0 820 270" role="img" aria-label={`${asset} ${t.market}`} onPointerMove={onMove} onPointerLeave={()=>setHover(null)}>
        <defs>
          <linearGradient id="chart-gradient" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={model.positive?'#4de8b0':'#ffad7a'} stopOpacity=".18"/>
            <stop offset="100%" stopColor={model.positive?'#4de8b0':'#ffad7a'} stopOpacity="0"/>
          </linearGradient>
        </defs>
        {[65,120,175,229].map(y=><line key={y} x1="22" y1={y} x2="798" y2={y} className="chart-grid"/>) }
        <path d={model.area} fill="url(#chart-gradient)"/>
        <path d={model.line} fill="none" stroke={model.positive?'#55edb6':'#ffad7a'} strokeWidth="2.7" strokeLinecap="round" strokeLinejoin="round"/>
        {model.marks.map(({trade,i,x,y})=><g key={i}>
          <circle cx={x} cy={y} r="10" fill={trade.kind==='PAPER_BUY'?'#1aa979':'#f0a34f'} stroke="#08151d" strokeWidth="3" />
          <path d={trade.kind==='PAPER_BUY'?`M${x-4} ${y+1} L${x} ${y-3} L${x+4} ${y+1}`:`M${x-4} ${y-1} L${x} ${y+3} L${x+4} ${y-1}`} fill="none" stroke="#fff" strokeWidth="1.8"/>
        </g>)}
        {selected?<>
          <line x1={selected.coords[0]} y1="22" x2={selected.coords[0]} y2="232" stroke="#d9fdf0" strokeOpacity=".5" strokeDasharray="4 6"/>
          <circle cx={selected.coords[0]} cy={selected.coords[1]} r="5" fill="#d9fdf0" stroke="#0b2430" strokeWidth="3"/>
        </>:<circle cx={last[0]} cy={last[1]} r="5" fill={model.positive?'#55edb6':'#ffad7a'} stroke="#082018" strokeWidth="3"/>}
      </svg>
      {selected&&<div className="chart-tooltip"><b>{formatMoney(selected.point.price_usdt,lang)}</b><span>{formatTimestamp(selected.point.timestamp,lang,{day:'2-digit',month:'short'})}</span></div>}
    </div>}
  </div>;
}

function useMiniNexData(asset, refreshNonce) {
  const [market,setMarket]=useState(null);
  const [paper,setPaper]=useState(null);
  const [history,setHistory]=useState([]);
  const [trades,setTrades]=useState([]);
  const [connected,setConnected]=useState(false);
  const [paperError,setPaperError]=useState(false);
  const [syncedAt,setSyncedAt]=useState(0);
  const activeRef=useRef(asset);
  activeRef.current=asset;

  useEffect(()=>{
    let live=true,ws=null,retryTimer=null,retries=0;
    const syncMarket=async()=>{
      try{const data=await getJson('/api/market/prices');if(live){setMarket(data);setSyncedAt(Date.now());}}catch{ /* WS / next poll will retry */ }
    };
    const syncPaper=async()=>{
      try{
        const data=ensurePaperOnly(await getJson('/api/paper/status'));
        if(live){setPaper(data);setPaperError(false);}
      }catch{
        if(live){setPaper(null);setPaperError(true);}
      }
    };
    const openStream=()=>{
      if(!live) return;
      try {
        const wsUrl=API_BASE.replace(/^http/,'ws')+'/ws/market';
        ws=new WebSocket(wsUrl);
        ws.onopen=()=>{if(live){setConnected(true);retries=0;}};
        ws.onmessage=event=>{
          try {const data=JSON.parse(event.data);
            if(live&&data?.markets?.BTC&&data?.markets?.ETH){setMarket(data);setSyncedAt(Date.now());}
          }catch { /* ignore invalid feed packets */ }
        };
        ws.onclose=()=>{
          if(!live)return;
          setConnected(false);
          const delay=Math.min(30000,1500*2**Math.min(retries++,5));
          retryTimer=window.setTimeout(openStream,delay);
        };
        ws.onerror=()=>{try{ws?.close();}catch{ /* connection is already closed */ }};
      }catch{setConnected(false);retryTimer=window.setTimeout(openStream,5000);}
    };
    syncMarket();syncPaper();openStream();
    const marketPoll=window.setInterval(()=>{if(!document.hidden)syncMarket();},12000);
    const paperPoll=window.setInterval(()=>{if(!document.hidden)syncPaper();},5000);
    return ()=>{live=false;window.clearInterval(marketPoll);window.clearInterval(paperPoll);window.clearTimeout(retryTimer);try{ws?.close();}catch{ /* done */ }};
  },[refreshNonce]);

  useEffect(()=>{
    let live=true;
    setHistory([]);setTrades([]);
    const sync=async()=>{
      try{
        const [h,r]=await Promise.all([getJson(`/api/market/history/${asset}?limit=3600`),getJson(`/api/paper/trades/${asset}?limit=50`)]);
        if(!live) return;
        setHistory(Array.isArray(h?.points)?h.points:[]);
        if(r?.mode==='PAPER_AUTO_DEMO'&&r.real_trading_enabled===false) setTrades(Array.isArray(r?.trades)?r.trades:[]);
      } catch { /* missing history is not a crash */ }
    };
    sync();const interval=window.setInterval(()=>{if(!document.hidden)sync();},12000);
    return()=>{live=false;window.clearInterval(interval);};
  },[asset,refreshNonce]);
  return {market,paper,history,trades,connected,paperError,syncedAt};
}

function usePersonalPaperTrades(asset, paperVisible, refreshNonce) {
  const {ready,authenticated,getAccessToken}=usePrivy();
  const [trades,setTrades]=useState([]);
  useEffect(()=>{
    let active=true;
    setTrades([]);
    if(!paperVisible||!ready||!authenticated)return()=>{active=false;};
    let inFlight=false;
    const sync=async()=>{
      if(inFlight)return;
      inFlight=true;
      try{
        const token=await getAccessToken();
        if(!token)throw new Error('Missing Privy token');
        const response=await fetch(API_BASE+'/api/user-paper/'+asset,{
          headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
        });
        if(!response.ok)throw new Error('Personal history unavailable');
        const data=await response.json();
        if(!active)return;
        if(data?.asset!==asset||data?.paper_only!==true||
            data?.real_trading_enabled!==false||data?.wallet_funds_used!==false){
          throw new Error('Untrusted personal paper response');
        }
        setTrades(Array.isArray(data.trades)?
          data.trades.filter(trade=>trade?.asset===asset&&
            (trade?.kind==='PAPER_BUY'||trade?.kind==='PAPER_SELL')&&
            trade?.real_trade===false):[]);
      }catch{
        if(active)setTrades([]);
      }finally{inFlight=false;}
    };
    sync();
    const interval=setInterval(sync,20000);
    return()=>{active=false;clearInterval(interval);};
  },[asset,paperVisible,ready,authenticated,getAccessToken,refreshNonce]);
  return trades;
}

/** Private PAPER-only portfolio snapshots for the compact Home card. */
function usePaperPortfolioHome(paperVisible, refreshNonce) {
  const {ready,authenticated,getAccessToken}=usePrivy();
  const [summary,setSummary]=useState(null);
  const [samples,setSamples]=useState([]);
  useEffect(()=>{
    let active=true, inFlight=false;
    setSummary(null);setSamples([]);
    if (!paperVisible || !ready || !authenticated) return ()=>{active=false;};
    const sync=async()=>{
      if(inFlight)return;
      inFlight=true;
      try{
        const token=await getAccessToken();
        if(!token)throw new Error('Login required');
        const rows=await Promise.all(['BTC','ETH'].map(async asset=>{
          const response=await fetch(API_BASE+'/api/user-paper/'+asset,{
            headers:{Authorization:'Bearer '+token,Accept:'application/json'},cache:'no-store'
          });
          if(!response.ok)throw new Error('Private paper data unavailable');
          return [asset,await response.json()];
        }));
        if(!active)return;
        const next=summarizePaperSessions(Object.fromEntries(rows));
        setSummary(next);
        if(next){
          setSamples(existing=>{
            const at=Date.now();
            if(existing.length && at-existing[existing.length-1].at<12000)return existing;
            return [...existing,{at,value:next.equity}].slice(-90);
          });
        }
      }catch{
        // Never display stale totals as a verified real wallet balance.
        if(active)setSummary(null);
      }finally{inFlight=false;}
    };
    sync();
    const interval=window.setInterval(sync,20000);
    return ()=>{active=false;window.clearInterval(interval);};
  },[ready,authenticated,getAccessToken,paperVisible,refreshNonce]);
  return {summary,samples,authenticated:ready&&authenticated};
}

function Navigation({page,setPage,t,mobile=false}) {
  return <nav className={mobile?'mobile-navigation':'side-navigation'} aria-label="Navigation">
    {NAV.map(({key,icon:Icon})=><button type="button" key={key} className={`nav-item ${page===key?'nav-item--active':''}`} onClick={()=>setPage(key)} aria-current={page===key?'page':undefined}>
      <Icon size={mobile?20:18} strokeWidth={2} /> <span>{mobile && key==='activity' ? t.compactSeeTrades : t[key]}</span>
    </button>)}
  </nav>;
}

function StatBox({label,value,sub,tone='',icon:Icon}) {
  return <div className="stat-box"><div className="stat-label">{Icon&&<Icon size={16}/>}<span>{label}</span></div><div className={`stat-value ${tone?`text-${tone}`:''}`}>{value}</div>{sub&&<div className="stat-sub">{sub}</div>}</div>;
}

function ActivityList({trades,t,lang,asset,compact=false}) {
  const shown=(Array.isArray(trades)?[...trades]:[]).reverse().slice(0,40);
  const hasMoreTrades=compact&&shown.length>4;
  if(!shown.length) return <div className="empty-activity"><div className="empty-activity-icon"><Activity size={24}/></div><strong>{t.emptyActivity}</strong><span>{t.emptyActivitySub}</span></div>;
  return <div className={'trade-list'+(hasMoreTrades?' trade-list--scroll':'')}
    tabIndex={hasMoreTrades?0:undefined}
    role={hasMoreTrades?'region':undefined}
    aria-label={hasMoreTrades?t.recentTrades+' · '+asset:undefined}>{shown.map((trade,i)=>{
    const buy=trade.kind==='PAPER_BUY';const pnl=trade.realized_net_profit;
    return <div key={`${trade.at||i}-${i}`} className="trade-row">
      <span className={`trade-icon ${buy?'trade-icon--buy':'trade-icon--sell'}`}>{buy?<ArrowDownLeft size={17}/>:<ArrowUpRight size={17}/>}</span>
      <div className="trade-main"><b>{buy?t.paperBuy:t.paperSell} · {asset}</b><span>{formatTimestamp(trade.at,lang,{day:'2-digit',month:'short'})} · {trade.reason||'—'}</span></div>
      <div className="trade-amount"><b>{formatMoney(buy?trade.total_spent:trade.net_proceeds,lang)}</b><span className={pnl!=null?(Number(pnl)>=0?'text-positive':'text-negative'):''}>{pnl!=null?formatSigned(pnl,lang):t.paperTrade}</span></div>
    </div>;
  })}</div>;
}

function MoneyInfo({portfolio,asset,t,lang,paper}) {
  const stable=portfolio?.settlement||'USDT';
  return <div className="balance-grid">
    <div className="balance-line"><span>{t.coins}</span><b>{formatCoin(portfolio?.coin_quantity,lang)} {asset}</b></div>
    <div className="balance-line"><span>{t.stable}</span><b>{formatMoney(portfolio?.stable_cash,lang)} {stable}</b></div>
    <div className="balance-line"><span>{t.costBasis}</span><b>{formatMoney(portfolio?.cost_basis,lang)}</b></div>
    <div className="balance-line"><span>{t.startingMode}</span><b>{portfolio?.starting_mode==='NATIVE_COINS'?t.startingCoin:t.startingStable}</b></div>
    <div className="balance-line"><span>{t.moves}</span><b>{portfolio?.trade_count??'—'}</b></div>
    <div className="balance-line"><span>{t.lastAction}</span><b>{portfolio?.last_trade_at?formatTimestamp(portfolio.last_trade_at,lang,{day:'2-digit',month:'short'}):t.never}</b></div>
  </div>;
}

function FeeDetails({paper,portfolio,asset,t,lang}) {
  const costs=paper?.estimated_costs||{};
  return <div className="cost-breakdown">
    <div className="balance-line"><span>{t.feeVenue}</span><b>{formatNumber(costs.venue_fee_pct,lang)} %</b></div>
    <div className="balance-line"><span>{t.feeSpread}</span><b>{formatNumber(costs.spread_pct,lang)} %</b></div>
    <div className="balance-line"><span>{t.feeSlippage}</span><b>{formatNumber(costs.slippage_pct,lang)} %</b></div>
    <div className="balance-line"><span>{t.feeNetwork}</span><b>{formatMoney(asset==='BTC'?costs.estimated_network_cost_btc_usd:costs.estimated_network_cost_eth_usd,lang)}</b></div>
    <div className="balance-line"><span>{t.costPaid}</span><b>{formatMoney((Number(portfolio?.buy_costs_paid_estimate)||0)+(Number(portfolio?.sell_costs_paid_estimate)||0),lang)}</b></div>
    <p className="small-note"><Info size={14}/>{t.feeDisclaimer}</p>
  </div>;
}

export default function App() {
  const [language,setLanguage]=useState(getLanguage);
  const [asset,setAsset]=useState('BTC');
  const [page,setPage]=useState('overview');
  const pageRef=useRef('overview');
  const previousPages=useRef([]);
  const helpRef=useRef(false);
  const backRef=useRef(null);
  const navigate=next=>{
    if(!['overview','activity','wallet','settings'].includes(next) || next===pageRef.current)return;
    previousPages.current.push(pageRef.current);
    pageRef.current=next;
    setPage(next);
  };
  const back=()=>{
    if(helpRef.current){
      helpRef.current=false;
      setHelpOpen(false);
      return;
    }
    const previous=previousPages.current.pop();
    const target=previous||'overview';
    if(target!==pageRef.current){
      pageRef.current=target;
      setPage(target);
    }
    // On Home, avoid accidentally closing the entire Android application.
  };
  backRef.current=back;
  useEffect(()=>{
    let disposed=false;
    let listener=null;
    if(Capacitor.isNativePlatform()){
      CapacitorApp.addListener('backButton',()=>backRef.current()).then(handle=>{
        if(disposed)handle.remove();
        else listener=handle;
      }).catch(()=>{});
    }
    const onPop=()=>backRef.current();
    window.addEventListener('popstate',onPop);
    return()=>{
      disposed=true;
      listener?.remove();
      window.removeEventListener('popstate',onPop);
    };
  },[]);
  const [period,setPeriod]=useState('m15');
  const [costsOpen,setCostsOpen]=useState(false);
  const [helpOpen,setHelpOpen]=useState(false);
  helpRef.current=helpOpen;
  const [refreshNonce,setRefreshNonce]=useState(0);
  const [paperVisible,setPaperVisible]=useState(false);
  useEffect(()=>{
    let active=true;
    getJson('/api/user-paper/availability')
      .then(data=>{if(active)setPaperVisible(data?.paper_available===true&&data?.live_trading_enabled===false);})
      .catch(()=>{if(active)setPaperVisible(false);});
    return()=>{active=false;};
  },[]);
  const {market,paper,history,connected,paperError,syncedAt}=useMiniNexData(asset,refreshNonce);
  const personalTrades=usePersonalPaperTrades(asset,paperVisible,refreshNonce);
  const paperHome=usePaperPortfolioHome(paperVisible,refreshNonce);
  const t=DICTIONARY[language];
  const mk=market?.markets?.[asset]||null;
  const valid=validMarket(mk) && Date.now()-syncedAt<FRESH_MS;
  const price=valid?mk.price_usdt:null;
  const change=valid?mk.change_24h_pct:null;
  const portfolio=paper?.portfolios?.[asset]||null;
  const workerOnline=!!(paper && paper.worker_error == null && paper.last_tick);
  const value=portfolioValue(portfolio);
  const realized=portfolio?.realized_net_pnl;
  const unrealized=portfolio?.estimated_unrealized_net_pnl;
  const Pnl=({n})=><span className={Number(n)>0?'text-positive':Number(n)<0?'text-negative':''}>{formatSigned(n,language)}</span>;
  const sourceTime=mk?.primary_at;
  const chooseLang=lang=>{setLanguage(lang);try{localStorage.setItem('mininex_language',lang);}catch{/* disabled */}};
  const name=asset==='BTC'?'Bitcoin':'Ethereum';

  useEffect(()=>{document.documentElement.lang=language;document.title=`MiniNex · ${asset} & ETH/BTC`;},[language,asset]);
  const tabKey=(name)=>()=>navigate(name);

  return <div className={`app-shell ${page==='overview' ? 'app-shell--home' : ''}`}>
    <aside className="sidebar">
      <div className="sidebar-brand"><MiniNexMark/><div><b>mini<span>nex</span></b><small>TRADING INTELLIGENCE</small></div></div>
      <Navigation page={page} setPage={navigate} t={t}/>
      
    </aside>

    <div className="content-column">
      <header className="topbar">
        <button className="mobile-brand" type="button" onClick={()=>navigate('overview')} aria-label={t.overview} title={t.overview}><MiniNexMark size={34}/><b>mini<span>nex</span></b></button>
        
        <div className="header-actions">
          <div className="lang-switch" role="group" aria-label={t.language}>
            <button type="button" className={language==='de'?'selected':''} onClick={()=>chooseLang('de')} aria-pressed={language==='de'}>DE</button>
            <button type="button" className={language==='en'?'selected':''} onClick={()=>chooseLang('en')} aria-pressed={language==='en'}>EN</button>
          </div>
          <button className="icon-btn mobile-settings-btn" type="button" aria-label={t.settings} title={t.settings} onClick={()=>navigate('settings')}><Settings2 size={17}/></button>
          <button className="icon-btn refresh-btn" type="button" aria-label={t.refresh} title={t.refresh} onClick={()=>setRefreshNonce(n=>n+1)}><RefreshCcw size={17}/></button>
        </div>
      </header>

      <main className="main-body">
        {page==='overview'&&<>
          <CompactOverview
            t={t} lang={language} chooseLang={chooseLang} asset={asset} setAsset={setAsset} period={period} setPeriod={setPeriod}
            history={history} trades={personalTrades} market={mk} marketSnapshot={market?.markets} valid={valid} portfolio={portfolio} showSimulatedTrades={paperVisible}
             portfolioSummary={paperHome.summary} portfolioSamples={paperHome.samples} portfolioAuthenticated={paperHome.authenticated}
            paper={paper} workerOnline={workerOnline} goTo={navigate}
          />
          <div className="desktop-overview">
          <section className="welcome-head"><h1>MiniNex</h1></section>
          <div className="asset-switch" role="tablist" aria-label="Asset">
            {ASSETS.map(a=><button type="button" key={a} role="tab" aria-selected={asset===a} className={`asset-tab ${asset===a?'asset-tab--active':''}`} onClick={()=>{setAsset(a);setPeriod('m15');}}>
              <AssetSymbol asset={a} size="sm"/><span>{a==='BTC'?'Bitcoin':'Ethereum'}</span>
            </button>)}
          </div>

          <div className="overview-grid">
            <div className="overview-main">
              <section className="panel market-panel">
                <div className="panel-top"><div><span className="section-eyebrow">{t.market}</span><h2>{name} <span className="symbol-label">{asset}/USDT</span></h2></div><Pill tone={valid?'teal':'orange'} icon={valid?Check:CircleAlert}>{valid?t.dataVerified:t.dataUnverified}</Pill></div>
                <div className="price-top"><div><div className="main-price">{formatMoney(price,language)}</div><span className={`market-change ${Number(change)>=0?'text-positive':'text-negative'}`}>{change!=null?`${Number(change)>0?'+':''}${formatNumber(change,language)}%`: '—'} <span>24h</span></span></div>
                  <div className="time-switch" role="group" aria-label="Chart period"><button type="button" className={period==='m15'?'active':''} onClick={()=>setPeriod('m15')}>{t.quarterHour}</button><button type="button" className={period==='h1'?'active':''} onClick={()=>setPeriod('h1')}>{t.hourly}</button><button type="button" disabled title={t.historyLimit}>{t.day}</button></div>
                </div>
                <PriceChart history={history} asset={asset} period={period} market={mk} trades={[]} t={t} lang={language}/>
                <div className="market-bottom"><div><span>{t.reference}</span><b>{valid?formatMoney(mk?.reference_price_usd,language):'—'}</b></div><div><span>{t.spreadCheck}</span><b>{valid?`${formatNumber(mk?.price_divergence_pct,language,3)} %`:'—'}</b></div><div><span>{t.lastUpdate}</span><b>{sourceTime?formatRelative(sourceTime,language):'—'}</b></div></div>
              </section>
              {paperVisible&&<section className="panel recent-panel" aria-label={t.recentTrades}>
                <div className="panel-top"><h2>{t.recentTrades} · {asset}</h2><span className="muted-description">{t.simulatedTradeLabel}</span></div>
                <ActivityList trades={personalTrades} t={t} lang={language} asset={asset} compact/>
              </section>}
            </div>
          </div>
          </div>
        </>}

        {page==='wallet'&&<>
          <section className="page-heading"><h1>{t.wallet}</h1></section>
          <PrivyWalletPanel t={t} lang={language} marketSnapshot={market?.markets} syncedAt={syncedAt}/>
          <UserPaperSessions t={t} lang={language}/>
        </>}

        {page==='activity'&&<>
          <section className="page-heading"><h1>{t.activity}</h1></section>
          <BlockchainPreview t={t}/>
          <ActivityReport t={t}/>
        </>}

        {page==='settings'&&<>
          <section className="page-heading settings-heading"><h1>{t.settings}</h1><button type="button" className="settings-info-button" aria-label={t.helpTitle} aria-expanded={helpOpen} aria-controls="mininex-help" onClick={()=>setHelpOpen(v=>!v)}><Info size={22}/></button></section>
          {helpOpen && <HelpCenter t={t}/>}

          <section className="panel settings-panel"><div className="settings-row"><span className="settings-icon"><Globe2 size={21}/></span><div><h2>{t.language}</h2></div></div>
            <div className="language-options"><button type="button" className={language==='de'?'chosen':''} onClick={()=>chooseLang('de')}><span>🇩🇪 Deutsch</span>{language==='de'&&<Check size={17}/>}</button><button type="button" className={language==='en'?'chosen':''} onClick={()=>chooseLang('en')}><span>🇬🇧 English</span>{language==='en'&&<Check size={17}/>}</button></div>
          </section>
          <section className="panel settings-panel"><div className="settings-row"><span className="settings-icon"><Wifi size={21}/></span><div><h2>{t.apiStatus}</h2></div></div>
            <div className="settings-state"><Pill tone={syncedAt&&Date.now()-syncedAt<FRESH_MS?'teal':'orange'} icon={syncedAt?Check:WifiOff}>{syncedAt&&Date.now()-syncedAt<FRESH_MS?t.apiConnected:t.apiDisconnected}</Pill></div>
          </section>
          <section className="panel settings-panel">
            <div className="settings-row">
              <span className="settings-icon"><History size={21}/></span>
              <div><h2>{t.activity}</h2></div>
            </div>
            <button className="settings-activity-link" type="button" onClick={()=>navigate('activity')}>
              <History size={17}/>{t.activity}<ChevronDown size={16} className="settings-activity-chevron"/>
            </button>
          </section>
          <UpdateStatus lang={language}/>
          <section className="panel settings-panel"><div className="settings-row"><span className="settings-icon"><LockKeyhole size={21}/></span><div><h2>{t.tradingUnavailable}</h2></div></div></section>
        </>}
      </main>
      
    </div>
    
  </div>;
}
