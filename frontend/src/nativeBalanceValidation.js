// Real SOL, XRP, TRX and DOGE holdings from independently linked native Privy wallets.
// Unavailable data is never treated as a zero or as simulated capital.
export const NATIVE_COINS=Object.freeze(['SOL','XRP','TRX','DOGE']);
export const NATIVE_NETWORKS=Object.freeze({
  SOL:'solana-mainnet',XRP:'xrpl-mainnet',TRX:'tron-mainnet',
  DOGE:'dogecoin-mainnet',
});
const AMOUNT=/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;

export function validNativeBalanceResponse(data){
  if(!data||typeof data!=='object'||Array.isArray(data)||
     data.mode!=='NATIVE_MAINNET_BALANCES_READ_ONLY'||
     data.wallet_ownership_verified!==true||
     data.wallet_funds_used_for_paper!==false||
     data.real_trading_enabled!==false||
     data.execution_available!==false||
     !Array.isArray(data.asset_order)||
     data.asset_order.length!==NATIVE_COINS.length||
     !data.asset_order.every((coin,i)=>coin===NATIVE_COINS[i])||
     !data.assets||typeof data.assets!=='object'||Array.isArray(data.assets))
    return false;
  return NATIVE_COINS.every(coin=>{
    const item=data.assets[coin];
    return item&&typeof item==='object'&&!Array.isArray(item)&&
      item.asset===coin&&item.network===NATIVE_NETWORKS[coin]&&
      item.native===true&&item.wallet_funds_used_for_paper===false&&(
        (['unavailable','wallet_missing'].includes(item.status)&&item.balance===null)||
        (item.status==='verified'&&typeof item.balance==='string'&&
         item.balance.length<=85&&AMOUNT.test(item.balance))
      );
  });
}
