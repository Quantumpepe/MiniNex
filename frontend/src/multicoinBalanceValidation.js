// Five additional REAL blockchain holdings. These are never PAPER balances
// or USD valuations; missing RPC data is shown as unavailable, not zero.
export const EVM_BALANCE_ASSETS=Object.freeze(['BNB','POL','AVAX','LINK','UNI']);
export const EVM_BALANCE_NETWORKS=Object.freeze({
  BNB:'bsc-mainnet',POL:'polygon-pos-mainnet',
  AVAX:'avalanche-c-mainnet',LINK:'ethereum-mainnet',UNI:'ethereum-mainnet',
});
export const EVM_BALANCE_CHAIN_IDS=Object.freeze({BNB:56,POL:137,AVAX:43114,LINK:1,UNI:1});
const DECIMAL=/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?$/;

export function validateMulticoinBalanceResponse(data){
  if(!data||typeof data!=='object'||Array.isArray(data)||
     data.mode!=='REAL_MAINNET_BALANCES_READ_ONLY'||
     data.wallet_ownership_verified!==true||
     data.wallet_funds_used_for_paper!==false||
     data.real_trading_enabled!==false||
     data.execution_available!==false||
     !Array.isArray(data.asset_order)||
     data.asset_order.length!==EVM_BALANCE_ASSETS.length||
     !data.asset_order.every((asset,i)=>asset===EVM_BALANCE_ASSETS[i])||
     !data.assets||typeof data.assets!=='object')return false;

  return EVM_BALANCE_ASSETS.every(asset=>{
    const row=data.assets[asset];
    const isToken=asset==='LINK'||asset==='UNI';
    return row&&row.asset===asset&&
      row.network===EVM_BALANCE_NETWORKS[asset]&&
      // Accept the previous API during rolling deployment, but reject conflicting metadata.
      (row.chain_id===undefined||row.chain_id===EVM_BALANCE_CHAIN_IDS[asset])&&
      (row.decimals===undefined||row.decimals===18)&&
      row.native===!isToken&&
      row.wallet_funds_used_for_paper===false&&
      (row.status==='unavailable'&&row.balance===null||
       row.status==='verified'&&typeof row.balance==='string'&&
       row.balance.length<=85&&DECIMAL.test(row.balance));
  });
}
