/** Identity-scoped PAPER valuation. Never conflates virtual equity with a real wallet. */
const ASSETS = ['BTC','ETH'];

const finite = value => {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

/** Only verified estimated liquidation values count towards displayed equity. */
export function summarizePaperSessions(sessions) {
  if (!sessions || typeof sessions !== 'object') return null;
  let equity = 0;
  let startingCapital = 0;
  let activeAssets = 0;
  let runningAssets = 0;
  for (const asset of ASSETS) {
    const row = sessions[asset];
    if (!row || row.status === 'NOT_STARTED') continue;
    if (row.asset !== asset || row.paper_only !== true ||
        row.real_trading_enabled !== false || row.wallet_funds_used !== false ||
        !['ACTIVE','PAUSED'].includes(row.status)) return null;
    const initial = finite(row.demo_start_cash);
    const valuation = row.position?.valuation;
    const cash = finite(row.position?.stable_cash);
    const coin = finite(row.position?.coin_quantity);
    const estimated = finite(valuation?.estimated_total_equity_usd);
    if (initial === null || initial <= 0 ||
        cash === null || cash < 0 || coin === null || coin < 0 ||
        valuation?.quote_valid !== true || estimated === null || estimated < 0) return null;
    // A read-only valuation already accounts for estimated exit costs; do not add
    // coin value or subtract acquisition fees again.
    equity += estimated;
    startingCapital += initial;
    activeAssets++;
    if (row.status === 'ACTIVE') runningAssets++;
  }
  if (!activeAssets) return null;
  const pnl = equity - startingCapital;
  return {
    equity: Math.round(equity * 100) / 100,
    startingCapital: Math.round(startingCapital * 100) / 100,
    pnl: Math.round(pnl * 100) / 100,
    pnlPct: startingCapital > 0 ? pnl / startingCapital * 100 : 0,
    assetCount: activeAssets,
    runningAssets,
    mode: 'PAPER'
  };
}

/** Show only observed portfolio snapshots; never fabricate a historical curve. */
export function paperSparkline(samples) {
  if (!Array.isArray(samples) || samples.length < 2) return null;
  const points = samples.filter(p => p && Number.isFinite(p.at) && Number.isFinite(p.value) && p.value >= 0);
  if (points.length < 2) return null;
  const values = points.map(p=>p.value), lo=Math.min(...values), hi=Math.max(...values);
  const spread=Math.max(hi-lo,Math.max(0.000001,Math.abs(hi)*0.0002));
  const first=points[0].at, last=points[points.length-1].at;
  const x=(p,i)=>8 + (last > first ? (p.at-first)/(last-first) : i/(points.length-1))*184;
  const y=p=>36 - ((p.value-lo+(spread-(hi-lo))/2)/spread)*24;
  const line=points.map((p,i)=>(i?'L':'M')+x(p,i).toFixed(1)+' '+y(p).toFixed(1)).join(' ');
  return {line, area:line+' L192 45 L8 45 Z', lastY:y(points[points.length-1]),
          positive:values[values.length-1]>=values[0]};
}


/** Exactly the 12 opt-in, independent PAPER portfolios. Never mix real wallet funds. */
export const HOME_PAPER_PORTFOLIO_COINS=Object.freeze([
  'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','QNT','UNI','ARB',
]);

const EXTRA_COINS=HOME_PAPER_PORTFOLIO_COINS.slice(2);
const isMoney=n=>typeof n==='number'&&Number.isFinite(n)&&n>=0;
const roundMoney=n=>Math.round(n*100)/100;

function coreEquity(row,asset) {
  if(!row||row.paper_only!==true||
     row.real_trading_enabled!==false||row.wallet_funds_used!==false)
    return undefined;
  // Unstarted BTC/ETH records intentionally contain no asset field.
  if(row.status==='NOT_STARTED')return null;
  if(row.asset!==asset)return undefined;
  if(!['ACTIVE','PAUSED'].includes(row.status))return undefined;
  const start=finite(row.demo_start_cash);
  const qty=finite(row.position?.coin_quantity);
  const cash=finite(row.position?.stable_cash);
  if(start===null||start<=0||cash===null||cash<0||qty===null||qty<0)
    return undefined;
  const quoteValid=row.position?.valuation?.quote_valid===true;
  const equity=quoteValid?finite(row.position?.valuation?.estimated_total_equity_usd):null;
  if(quoteValid&&(equity===null||equity<0))return undefined;
  return {start,equity,valid:quoteValid,running:row.status==='ACTIVE'};
}

function extraEquity(row,asset){
  if(!row||row.asset!==asset) return undefined;
  if(row.status==='NOT_STARTED')return null;
  if(!['ACTIVE','PAUSED'].includes(row.status))return undefined;
  const start=row.budget_usd;
  if(!isMoney(start)||start<=0) return undefined;
  const equity=row.estimated_equity_usd;
  const valid=row.quote_valid===true&&isMoney(equity);
  return {start,equity:valid?equity:null,valid,running:row.status==='ACTIVE'};
}

/** Summary for a selected coin or the combined ALL portfolio.
 *  Quote gaps leave capital visible but never create a false total/PnL.
 */
export function summarizeHomePaperPortfolio(core,extraReport,scope){
  if(scope!=='ALL'&&!HOME_PAPER_PORTFOLIO_COINS.includes(scope))
    return null;
  const target=scope==='ALL'?HOME_PAPER_PORTFOLIO_COINS:[scope];
  const needsExtra=target.some(coin=>EXTRA_COINS.includes(coin));
  if(needsExtra){
    if(!extraReport||extraReport.paper_only!==true||
       extraReport.simulation_only!==true||
       extraReport.real_trading_enabled!==false||
       extraReport.wallet_funds_used!==false||
       !Array.isArray(extraReport.assets)||
       extraReport.assets.length!==EXTRA_COINS.length||
       !extraReport.assets.every((row,i)=>row?.asset===EXTRA_COINS[i]))
      return null;
  }
  let started=0,running=0,startingCapital=0,equity=0,unverified=false;
  for(const coin of target){
    const record=EXTRA_COINS.includes(coin)?
      extraEquity(extraReport?.assets.find(x=>x.asset===coin),coin):
      coreEquity(core?.[coin],coin);
    if(record===undefined)return null;
    if(record===null)continue;
    started++;
    startingCapital+=record.start;
    if(record.running)running++;
    if(!record.valid)unverified=true;
    else equity+=record.equity;
  }
  if(!started)return null;
  // Every started coin must have a verified mark for the combined total.
  // The starting capital is always shown as a distinct virtual budget.
  const valid=!unverified;
  const pnl=valid?equity-startingCapital:null;
  return {
    scope,startingCapital:roundMoney(startingCapital),
    equity:valid?roundMoney(equity):null,
    pnl:valid?roundMoney(pnl):null,
    pnlPct:valid&&startingCapital>0?pnl/startingCapital*100:null,
    valuationAvailable:valid,assetCount:started,runningAssets:running,
    mode:'PAPER',
  };
}

/**
 * Bounded, user-scoped observed PAPER equity history for the home chart.
 * This is NOT a market-price curve or an invented portfolio backtest.
 * Save only virtual ALL-coin totals; never cache LIVE wallet holdings.
 * Local app data can be lost when storage is cleared or app is reinstalled.
 */
const PAPER_HISTORY_PREFIX='mininex:paper-equity:v1:';
const PAPER_HISTORY_MAX_AGE_MS=24*60*60*1000;
const PAPER_HISTORY_BUCKET_MS=30*1000;
const PAPER_HISTORY_MAX_POINTS=2880;
const paperHistoryKey=(did,scope)=>
  typeof did==='string'&&did.startsWith('did:privy:')&&did.length<=200&&
  (scope==='ALL'||HOME_PAPER_PORTFOLIO_COINS.includes(scope))
    ? PAPER_HISTORY_PREFIX+encodeURIComponent(did)+':'+scope:null;

export function readObservedPaperEquity(did,scope,storage,now=Date.now()){
  try{
    const key=paperHistoryKey(did,scope);
    if(!key||!Number.isSafeInteger(now))return [];
    const store=storage??(typeof window!=='undefined'?window.localStorage:null);
    const raw=store?.getItem(key);
    if(typeof raw!=='string'||raw.length>350000)return [];
    const record=JSON.parse(raw);
    if(record?.version!==1||record.scope!==scope||!Array.isArray(record.points))
      return [];
    const filtered=[];
    let previousAt=-Infinity;
    for(const point of record.points.slice(-PAPER_HISTORY_MAX_POINTS)){
      if(!point||typeof point!=='object'||
         !Number.isSafeInteger(point.at)||point.at<=previousAt||
         point.at<now-PAPER_HISTORY_MAX_AGE_MS||point.at>now||
         typeof point.value!=='number'||!Number.isFinite(point.value)||point.value<0||
         typeof point.capital!=='number'||!Number.isFinite(point.capital)||
         point.capital<=0)continue;
      filtered.push({at:point.at,value:point.value,capital:point.capital});
      previousAt=point.at;
    }
    return filtered;
  }catch{return [];}
}

export function appendObservedPaperEquity(previous,summary,now=Date.now()){
  if(!summary||summary.mode!=='PAPER'||
     summary.valuationAvailable!==true||!Number.isSafeInteger(now)||
     typeof summary.equity!=='number'||!Number.isFinite(summary.equity)||
     summary.equity<0||typeof summary.startingCapital!=='number'||
     !Number.isFinite(summary.startingCapital)||summary.startingCapital<=0)
    return Array.isArray(previous)?previous:[];
  const capital=summary.startingCapital;
  const points=(Array.isArray(previous)?previous:[]).filter(p=>
    p&&Number.isSafeInteger(p.at)&&p.at<=now&&
    p.at>=now-PAPER_HISTORY_MAX_AGE_MS&&
    typeof p.value==='number'&&Number.isFinite(p.value)&&p.value>=0&&
    p.capital===capital);
  const sample={at:now,value:summary.equity,capital};
  const last=points.at(-1);
  if(last&&Math.floor(last.at/PAPER_HISTORY_BUCKET_MS)===
      Math.floor(now/PAPER_HISTORY_BUCKET_MS)){
    // Keep the latest REAL reading in each 30-second bucket, avoiding
    // hundreds of tiny points that change the visual trend every refresh.
    return [...points.slice(0,-1),sample].slice(-PAPER_HISTORY_MAX_POINTS);
  }
  if(last&&last.at>=now)return points;
  return [...points,sample].slice(-PAPER_HISTORY_MAX_POINTS);
}

export function saveObservedPaperEquity(did,scope,points,storage){
  try{
    const key=paperHistoryKey(did,scope);
    if(!key||!Array.isArray(points)||!points.length)return false;
    const store=storage??(typeof window!=='undefined'?window.localStorage:null);
    if(!store)return false;
    store.setItem(key,JSON.stringify({version:1,scope,
      points:points.slice(-PAPER_HISTORY_MAX_POINTS)}));
    return true;
  }catch{return false;}
}
