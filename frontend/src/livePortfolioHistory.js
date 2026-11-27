/** Private local observations, not a reconstructed market chart.
 * Only aggregate USD values are stored; no addresses or coin balances.
 * Identity and mode are distinct from PAPER storage.
 */
const PREFIX='mininex:live-equity:v2:';
const AGE=24*60*60*1000, BUCKET=30000, MAX=2880;
const key=did=>typeof did==='string'&&did.startsWith('did:privy:')&&did.length<=256
  ?PREFIX+encodeURIComponent(did):null;
const valid=(p,now)=>p&&Number.isSafeInteger(p.at)&&p.at<=now&&p.at>=now-AGE&&
  typeof p.value==='number'&&Number.isFinite(p.value)&&p.value>=0&&
  typeof p.coverage==='string'&&p.coverage.length>0&&p.coverage.length<=512;
export function readLiveEquity(did,storage,now=Date.now()){
  try{
    const k=key(did),store=storage??window.localStorage;
    if(!k)return [];
    const raw=store.getItem(k);
    if(typeof raw!=='string'||raw.length>2000000)return [];
    const record=JSON.parse(raw);
    if(record?.version!==2||!Array.isArray(record.points))return [];
    let last=-Infinity;
    return record.points.slice(-MAX).filter(p=>{
      if(!valid(p,now)||p.at<=last)return false;
      last=p.at;return true;
    }).map(({at,value,coverage})=>({at,value,coverage}));
  }catch{return [];}
}
export function appendLiveEquity(points,summary,now=Date.now()){
  if(summary?.mode!=='LIVE_WALLET'||summary.valuationAvailable!==true||
     typeof summary.coverage!=='string'||!summary.coverage||summary.coverage.length>512||
     !Number.isSafeInteger(now)||
     typeof summary.observedEquityUsd!=='number'||!Number.isFinite(summary.observedEquityUsd)||
     summary.observedEquityUsd<0)return points;
  const next=points.filter(p=>valid(p,now));
  const sample={at:now,value:summary.observedEquityUsd,coverage:summary.coverage};
  const last=next.at(-1);
  if(last&&last.at>=now)return next;
  if(last&&last.coverage===sample.coverage&&Math.floor(last.at/BUCKET)===Math.floor(now/BUCKET))
    return [...next.slice(0,-1),sample].slice(-MAX);
  return [...next,sample].slice(-MAX);
}
export function saveLiveEquity(did,points,storage){
  try{
    const k=key(did),store=storage??window.localStorage;
    if(!k||!Array.isArray(points)||!points.length)return false;
    store.setItem(k,JSON.stringify({version:2,points:points.slice(-MAX)}));
    return true;
  }catch{return false;}
}

// LIVE-only chart. The time axis always spans the same interval, and a
// minimum 2% vertical range keeps fractional-cent movements proportional.
// Coverage changes and observation gaps are breaks, never fabricated trades.
export function livePortfolioSparkline(samples,{recentMs=15*60*1000}={}){
  if(!Array.isArray(samples)||!Number.isFinite(recentMs)||recentMs<=0)return null;
  const ordered=samples.filter(p=>p&&Number.isFinite(p.at)&&Number.isFinite(p.value)&&
    p.value>=0&&typeof p.coverage==='string'&&p.coverage.length>0).sort((a,b)=>a.at-b.at);
  const last=ordered.at(-1);
  if(!last)return null;
  const start=last.at-recentMs;
  const points=ordered.filter(p=>p.at>=start&&p.coverage===last.coverage);
  const values=points.map(p=>p.value),lo=Math.min(...values),hi=Math.max(...values);
  const spread=Math.max(hi-lo,Math.max(0.01,hi*0.02));
  const x=p=>8+(p.at-start)/recentMs*184;
  const y=p=>36-((p.value-lo+(spread-(hi-lo))/2)/spread)*24;
  const segments=[];
  let segment=[],previous=null;
  for(const point of ordered){
    if(point.at<start)continue;
    if(point.coverage!==last.coverage||previous&&point.at-previous.at>120000){
      if(segment.length)segments.push(segment);
      segment=[];
    }
    if(point.coverage===last.coverage)segment.push(point);
    previous=point;
  }
  if(segment.length)segments.push(segment);
  const path=group=>group.map((p,i)=>(i?'L':'M')+x(p).toFixed(1)+' '+y(p).toFixed(1)).join(' ');
  return {line:segments.map(path).join(' '),
    area:segments.filter(group=>group.length>1).map(group=>path(group)+' L'+x(group.at(-1)).toFixed(1)+' 45 L'+x(group[0]).toFixed(1)+' 45 Z').join(' '),
    lastY:y(last),count:points.length,positive:last.value>=values[0]};
}
