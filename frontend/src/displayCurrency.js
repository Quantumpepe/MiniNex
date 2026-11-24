// Currency conversion is presentation only; never used in trading.
export const CURRENCIES=Object.freeze(['USD','CHF','EUR','GBP','JPY']);
const PREF='mininex_display_currency';
let currency='USD', rates=null;
export function getDisplayCurrency(){return currency;}
export function readCurrencyPreference(){
  try{const c=localStorage.getItem(PREF);return CURRENCIES.includes(c)?c:'USD';}
  catch{return 'USD';}
}
export function setDisplayCurrency(code,{persist=true}={}){
  if(!CURRENCIES.includes(code))return false;
  currency=code;
  if(persist)try{localStorage.setItem(PREF,code);}catch{}
  return true;
}
export function setDisplayRates(data){
  if(data?.base!=='USD'||data.reference_only!==true||data.rates?.USD!==1||
     !CURRENCIES.slice(1).every(c=>typeof data.rates[c]==='number'&&
       Number.isFinite(data.rates[c])&&data.rates[c]>0&&
       /^\d{4}-\d{2}-\d{2}$/.test(data.dates?.[c]||'')))return false;
  rates={...data.rates};
  return true;
}
export function displayRate(code=currency){return code==='USD'?1:rates?.[code]??null;}
export function displayCurrencySymbol(code=currency){
  return {USD:'$',CHF:'CHF',EUR:'€',GBP:'£',JPY:'¥'}[code]||code;
}
export function convertedDisplayAmount(usd,code=currency){
  const r=displayRate(code);
  return r!==null&&typeof usd==='number'&&Number.isFinite(usd)&&
    Number.isFinite(usd*r)?usd*r:null;
}
