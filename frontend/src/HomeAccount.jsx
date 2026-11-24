import React,{useEffect,useRef,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {UserRound,LogOut} from 'lucide-react';
import {PRIVY_APP_ID} from './privyConfig.js';

function PrivyHomeAccount({t}){
  const {ready,authenticated,login,logout}=usePrivy();
  const [open,setOpen]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState(false);
  const root=useRef(null);
  const pending=useRef(false);
  useEffect(()=>{setOpen(false);setError(false);},[authenticated]);
  useEffect(()=>{
    if(!open&&!error)return;
    const outside=event=>{
      if(!root.current?.contains(event.target)){setOpen(false);setError(false);}
    };
    const escape=event=>{
      if(event.key==='Escape'){setOpen(false);setError(false);}
    };
    document.addEventListener('pointerdown',outside);
    document.addEventListener('keydown',escape);
    return ()=>{
      document.removeEventListener('pointerdown',outside);
      document.removeEventListener('keydown',escape);
    };
  },[open,error]);
  const run=async(action)=>{
    if(!ready||pending.current)return;
    pending.current=true;setBusy(true);setError(false);
    try{await action();setOpen(false);}
    catch{setError(true);}
    finally{pending.current=false;setBusy(false);}
  };
  const label=authenticated?t.homeAccountSignedIn:t.homeAccountLogin;
  return <div className="home-account" ref={root}>
    <button type="button" className={'home-account-button'+(authenticated?' home-account-button--signed':'')}
      aria-label={label} title={label} disabled={!ready||busy}
      aria-expanded={authenticated?open:undefined}
      onClick={()=>authenticated?setOpen(v=>!v):void run(login)}>
      <UserRound size={15} aria-hidden="true"/>
      {!authenticated&&<span>{t.homeAccountLogin}</span>}
    </button>
    {(open||error)&&<div className="home-account-menu">
      {authenticated&&open&&<button type="button" disabled={busy} onClick={()=>void run(logout)}>
        <LogOut size={13} aria-hidden="true"/> {t.privyLogout}
      </button>}
      {error&&<span role="alert" className="home-account-error">{t.privyError}</span>}
    </div>}
  </div>;
}

export default function HomeAccount({t}){
  if(!PRIVY_APP_ID)return null;
  return <PrivyHomeAccount t={t}/>;
}
