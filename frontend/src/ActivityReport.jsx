import React, {useEffect, useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {API_BASE} from './data.js';
import {PRIVY_APP_ID} from './privyConfig.js';

function VerifiedActivity({t}) {
  const {ready, authenticated, getAccessToken, login} = usePrivy();
  const [year,setYear]=useState(new Date().getFullYear());
  const [state,setState]=useState('idle');
  const [activity,setActivity]=useState(null);
  const [busy,setBusy]=useState('');
  const years=Array.from({length:Math.max(1,new Date().getFullYear()-2020+1)},(_,i)=>new Date().getFullYear()-i);

  useEffect(()=>{
    if(!ready||!authenticated){setActivity(null);setState('idle');return;}
    let active=true;
    setActivity(null);
    setState('loading');
    (async()=>{
      try{
        const token=await getAccessToken();
        if(!token)throw new Error('No token');
        const response=await fetch(API_BASE+'/api/activity/'+year,{
          method:'GET',headers:{Authorization:'Bearer '+token,Accept:'application/json'},
          cache:'no-store'
        });
        if(!response.ok)throw new Error('Unavailable');
        const data=await response.json();
        if(data?.verified_only!==true||data?.paper_excluded!==true||
           data?.tax_complete!==false||!Array.isArray(data.rows))throw new Error('Bad activity');
        if(active){setActivity(data);setState('ready');}
      }catch{if(active){setActivity(null);setState('failed');}}
    })();
    return()=>{active=false;};
  },[year,ready,authenticated,getAccessToken]);

  const exportYear=async format=>{
    if(busy||!authenticated||state!=='ready'||!['csv','pdf'].includes(format))return;
    setBusy(format);
    try{
      const token=await getAccessToken();
      if(!token)throw new Error('Not signed in');
      const response=await fetch(API_BASE+'/api/activity/'+year+'/export/'+format,{
        method:'GET',headers:{Authorization:'Bearer '+token},cache:'no-store'
      });
      if(!response.ok)throw new Error('Export unavailable');
      const blob=await response.blob();
      const url=URL.createObjectURL(blob);
      const a=document.createElement('a');
      a.href=url;
      a.download='MiniNex_Activity_'+year+'.'+format;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(()=>URL.revokeObjectURL(url),60000);
    }catch{setState('failed');}
    finally{setBusy('');}
  };

  return <section className="panel activity-report">
    <div className="activity-report-heading"><h2>{t.taxAnnualReport}</h2>
      <select aria-label={t.taxYear} value={year} onChange={e=>setYear(Number(e.target.value))}>
        {years.map(y=><option key={y} value={y}>{y}</option>)}
      </select>
    </div>
    {!ready?<p>{t.privyLoading}</p>:
      !authenticated?<button className="privy-wallet-action" type="button" onClick={login}>{t.privyLogin}</button>:<>
        <p>{t.taxExportNote}</p>
        <div className="tax-export-actions">
          <button type="button" onClick={()=>exportYear('pdf')} disabled={state!=='ready'||!!busy}>{t.taxDownloadPdf}</button>
          <button type="button" onClick={()=>exportYear('csv')} disabled={state!=='ready'||!!busy}>{t.taxDownloadCsv}</button>
        </div>
        {state==='loading'&&<p>{t.taxLoading}</p>}
        {state==='failed'&&<p role="alert">{t.taxError}</p>}
        {state==='ready'&&<p>{activity?.count ? t.taxCount.replace('{count}',String(activity.count)) : t.taxNoRecords}</p>}
        {state==='ready'&&activity?.rows?.length>0&&<div className="tax-activity-list">
          {activity.rows.slice(-20).reverse().map(row=><div className="tax-activity-row"
            key={row.source+'-'+row.record_id}>
            <div><strong>{row.type} · {row.asset}</strong><small>{row.date_utc?.slice(0,16).replace('T',' ')||''} UTC</small></div>
            <div><strong>{row.quantity} {row.asset}</strong><small>{row.source==='BLOCKCHAIN'?t.taxBlockchain:t.taxMiniNexTrade}</small></div>
          </div>)}
        </div>}
      </>}
  </section>;
}

export default function ActivityReport({t}) {
  if(!PRIVY_APP_ID)return <section className="panel activity-report"><h2>{t.taxAnnualReport}</h2><p>{t.taxLoginRequired}</p></section>;
  return <VerifiedActivity t={t}/>;
}
