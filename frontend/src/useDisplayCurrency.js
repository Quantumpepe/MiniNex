import {useEffect,useState} from 'react';
import {API_BASE} from './data.js';
import {readCurrencyPreference,setDisplayCurrency,setDisplayRates,displayRate} from './displayCurrency.js';

export default function useDisplayCurrency(){
  const [currency,setCurrency]=useState(()=>{
    const value=readCurrencyPreference();
    setDisplayCurrency(value,{persist:false});
    return value;
  });
  const [reference,setReference]=useState(null);
  const [failed,setFailed]=useState(false);
  const chooseCurrency=code=>{
    if(setDisplayCurrency(code))setCurrency(code);
  };
  useEffect(()=>{
    let active=true,timer,controller;
    const sync=async()=>{
      controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),10000);
      try{
        const response=await fetch(API_BASE+'/api/display-currency',{
          signal:controller.signal,headers:{Accept:'application/json'},cache:'no-store'});
        if(!response.ok)throw Error('FX unavailable');
        const data=await response.json();
        if(active){
          if(!setDisplayRates(data))throw Error('Invalid FX rates');
          setReference(data);setFailed(false);
        }
      }catch{if(active)setFailed(true);}
      finally{clearTimeout(timeout);if(active)timer=setTimeout(sync,3600000);}
    };
    sync();
    return()=>{active=false;clearTimeout(timer);controller?.abort();};
  },[]);
  return {currency,chooseCurrency,reference,failed,available:displayRate(currency)!==null};
}
