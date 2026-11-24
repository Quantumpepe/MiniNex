/** Private local observations, not a reconstructed market chart.
 * Only aggregate USD values are stored; no addresses or coin balances.
 * Identity and mode are distinct from PAPER storage.
 */
const PREFIX='mininex:live-equity:v1:';
const AGE=24*60*60*1000, BUCKET=30000, MAX=2880;
const key=did=>typeof did==='string'&&did.startsWith('did:privy:')&&did.length<=256
  ?PREFIX+encodeURIComponent(did):null;
const valid=(p,now)=>p&&Number.isSafeInteger(p.at)&&p.at<=now&&p.at>=now-AGE&&
  typeof p.value==='number'&&Number.isFinite(p.value)&&p.value>=0;
export function readLiveEquity(did,storage,now=Date.now()){
  try{
    const k=key(did),store=storage??window.localStorage;
    if(!k)return [];
    const raw=store.getItem(k);
    if(typeof raw!=='string'||raw.length>300000)return [];
    const record=JSON.parse(raw);
    if(record?.version!==1||!Array.isArray(record.points))return [];
    let last=-Infinity;
    return record.points.slice(-MAX).filter(p=>{
      if(!valid(p,now)||p.at<=last)return false;
      last=p.at;return true;
    }).map(({at,value})=>({at,value}));
  }catch{return [];}
}
export function appendLiveEquity(points,summary,now=Date.now()){
  if(summary?.mode!=='LIVE_WALLET'||summary.valuationAvailable!==true||
     summary.incomplete!==false||!Number.isSafeInteger(now)||
     typeof summary.observedEquityUsd!=='number'||!Number.isFinite(summary.observedEquityUsd)||
     summary.observedEquityUsd<0)return points;
  const next=points.filter(p=>valid(p,now));
  const sample={at:now,value:summary.observedEquityUsd};
  const last=next.at(-1);
  if(last&&last.at>=now)return next;
  if(last&&Math.floor(last.at/BUCKET)===Math.floor(now/BUCKET))
    return [...next.slice(0,-1),sample].slice(-MAX);
  return [...next,sample].slice(-MAX);
}
export function saveLiveEquity(did,points,storage){
  try{
    const k=key(did),store=storage??window.localStorage;
    if(!k||!Array.isArray(points)||!points.length)return false;
    store.setItem(k,JSON.stringify({version:1,points:points.slice(-MAX)}));
    return true;
  }catch{return false;}
}
