import {COINS,coinMarketSnapshot} from './coins.js';
import {validateWalletBalanceResponse} from './walletBalanceValidation.js';
import {validateMulticoinBalanceResponse} from './multicoinBalanceValidation.js';
import {validNativeBalanceResponse} from './nativeBalanceValidation.js';

// Add native and wrapped-mainnet units without rounding the underlying
// 18-decimal blockchain amounts. A failed wrapped RPC read is UNKNOWN, not 0.
const UNIT_18=/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?$/;
const WRAPPED_NATIVE=Object.freeze({ETH:'WETH',BNB:'WBNB',POL:'WPOL',AVAX:'WAVAX'});
function toAtomic(row){
  if(row?.status!=='verified'||typeof row.balance!=='string'||
     !UNIT_18.test(row.balance))return null;
  const [whole,fraction='']=row.balance.split('.');
  return BigInt(whole)*10n**18n+BigInt(fraction.padEnd(18,'0')||'0');
}
function atomicDisplay(atomic){
  const whole=(atomic/10n**18n).toString();
  const decimal=(atomic%10n**18n).toString().padStart(18,'0').replace(/0+$/,'');
  return decimal?whole+'.'+decimal:whole;
}
export function combineVerifiedNativeAndWrapped(native,wrapped){
  const n=toAtomic(native),w=toAtomic(wrapped);
  if(n===null&&w===null)return {status:'unavailable',balance:null,incomplete:true};
  return {status:'verified',balance:atomicDisplay((n??0n)+(w??0n)),
    incomplete:n===null||w===null};
}

// Verified LIVE wallet balances, never simulation budgets or traded profit.
// Failed wallet reads/price feeds must never silently become zero.
export function summarizeVerifiedWalletPortfolio(sources,markets,syncedAt,now=Date.now()){
  if(!sources||typeof sources!=='object')return null;
  const core=validateWalletBalanceResponse(sources.core)?sources.core.balances:null;
  const evmResponse=validateMulticoinBalanceResponse(sources.evm)?sources.evm:null;
  const evm=evmResponse?.assets||null;
  const wrapped=evmResponse?.wrapped_assets||null;
  const native=validNativeBalanceResponse(sources.native)?sources.native.assets:null;
  if(!core&&!evm&&!native)return null;
  let amountUSD=0, incomplete=!core||!evm||!native, positiveUnpriced=0, priced=0;
  const holdings=[];
  const read=(asset,entry,price)=>{
    if(!entry){incomplete=true;return;}
    if(entry.incomplete)incomplete=true;
    if(entry.status==='wallet_missing')return;
    if(entry.status!=='verified'){incomplete=true;return;}
    const quantity=Number(entry.balance);
    if(!Number.isFinite(quantity)||quantity<0){incomplete=true;return;}
    if(quantity===0)return;
    if(price===null){incomplete=true;positiveUnpriced++;return;}
    const value=quantity*price;
    if(!Number.isFinite(value)||value<0){incomplete=true;positiveUnpriced++;return;}
    amountUSD+=value;
    priced++;
    holdings.push({asset,balance:entry.balance,value});
  };
  for(const coin of COINS){
    const asset=coin.symbol;
    const entry=(['BTC','ETH'].includes(asset)?core:
      ['SOL','XRP','TRX'].includes(asset)?native:evm)?.[asset];
    // Wrapped units belong to the SAME underlying asset's real wallet value.
    // Do not hide the 25 WPOL after POL -> WPOL or count it twice.
    const held=WRAPPED_NATIVE[asset]
      ?combineVerifiedNativeAndWrapped(entry,wrapped?.[WRAPPED_NATIVE[asset]])
      :entry;
    const quote=coinMarketSnapshot(asset,markets,syncedAt,now);
    read(asset,held,quote?Number(quote.price_usdt):null);
  }
  for(const asset of ['USDC','USDT']){
    read(asset,core?.[asset],1); // Stablecoin USD reference, approximate.
  }
  holdings.sort((a,b)=>b.value-a.value);
  return {
    equity:priced>0||(!incomplete&&positiveUnpriced===0)?Math.round(amountUSD*100)/100:null,
    observedEquityUsd:priced>0||(!incomplete&&positiveUnpriced===0)?amountUSD:null,
    valuationAvailable:priced>0||(!incomplete&&positiveUnpriced===0),
    incomplete,holdings,positiveUnpriced,
    assetCount:holdings.length,mode:'LIVE_WALLET',currency:'USD',
  };
}
