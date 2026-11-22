import {COINS,coinMarketSnapshot} from './coins.js';
import {validateWalletBalanceResponse} from './walletBalanceValidation.js';
import {validateMulticoinBalanceResponse} from './multicoinBalanceValidation.js';
import {validNativeBalanceResponse} from './nativeBalanceValidation.js';

// Verified LIVE wallet balances, never simulation budgets or traded profit.
// Failed wallet reads/price feeds must never silently become zero.
export function summarizeVerifiedWalletPortfolio(sources,markets,syncedAt,now=Date.now()){
  if(!sources||typeof sources!=='object')return null;
  const core=validateWalletBalanceResponse(sources.core)?sources.core.balances:null;
  const evm=validateMulticoinBalanceResponse(sources.evm)?sources.evm.assets:null;
  const native=validNativeBalanceResponse(sources.native)?sources.native.assets:null;
  if(!core&&!evm&&!native)return null;
  let amountUSD=0, incomplete=!core||!evm||!native, positiveUnpriced=0, priced=0;
  const holdings=[];
  const read=(asset,entry,price)=>{
    if(!entry){incomplete=true;return;}
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
    const quote=coinMarketSnapshot(asset,markets,syncedAt,now);
    read(asset,entry,quote?Number(quote.price_usdt):null);
  }
  for(const asset of ['USDC','USDT']){
    read(asset,core?.[asset],1); // Stablecoin USD reference, approximate.
  }
  holdings.sort((a,b)=>b.value-a.value);
  return {
    equity:priced>0||(!incomplete&&positiveUnpriced===0)?Math.round(amountUSD*100)/100:null,
    valuationAvailable:priced>0||(!incomplete&&positiveUnpriced===0),
    incomplete,holdings,positiveUnpriced,
    assetCount:holdings.length,mode:'LIVE_WALLET',currency:'USD',
  };
}
