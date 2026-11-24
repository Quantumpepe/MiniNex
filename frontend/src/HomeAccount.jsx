import React,{useRef,useState} from 'react';
import {usePrivy} from '@privy-io/react-auth';
import {PRIVY_APP_ID} from './privyConfig.js';

function PrivyHomeAccount({t}){
  const {ready,authenticated,login,logout}=usePrivy();
  const [busy,setBusy]=useState(false);
  const pending=useRef(false);
  const run=async(action)=>{
    if(!ready||pending.current)return;
    pending.current=true;setBusy(true);
    try{await action();}
    catch{window.alert(t.privyError);}
    finally{pending.current=false;setBusy(false);}
  };
  const label=authenticated?t.privyLogout:t.homeAccountLogin;
  return <div className="home-account">
    <button type="button" className={'home-account-button'+(authenticated?' home-account-button--signed':'')}
      aria-label={label} title={label} disabled={!ready||busy} aria-busy={busy}
      onClick={()=>authenticated?void run(logout):void run(login)}>
      <span>{label}</span>
    </button>
  </div>;
}

export default function HomeAccount({t}){
  if(!PRIVY_APP_ID)return null;
  return <PrivyHomeAccount t={t}/>;
}
