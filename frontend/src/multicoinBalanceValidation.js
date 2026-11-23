// Seven additional REAL blockchain holdings. These are never PAPER balances
// or USD valuations; missing RPC data is shown as unavailable, not zero.
export const EVM_BALANCE_ASSETS=Object.freeze(['BNB','POL','AVAX','LINK','QNT','UNI','ARB']);
export const EVM_BALANCE_NETWORKS=Object.freeze({
  BNB:'bsc-mainnet',POL:'polygon-pos-mainnet',
  AVAX:'avalanche-c-mainnet',LINK:'ethereum-mainnet',QNT:'ethereum-mainnet',UNI:'ethereum-mainnet',
  ARB:'arbitrum-one-mainnet',
});
export const EVM_BALANCE_CHAIN_IDS=Object.freeze({BNB:56,POL:137,AVAX:43114,LINK:1,QNT:1,UNI:1,ARB:42161});
export const WRAPPED_EVM_ASSETS=Object.freeze({
  WBNB:{underlying:'BNB',network:'bsc-mainnet',chainId:56,
    contract:'0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'},
  WPOL:{underlying:'POL',network:'polygon-pos-mainnet',chainId:137,
    contract:'0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270'},
  WAVAX:{underlying:'AVAX',network:'avalanche-c-mainnet',chainId:43114,
    contract:'0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7'},
  WETH:{underlying:'ETH',network:'ethereum-mainnet',chainId:1,
    contract:'0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2'},
});
const DECIMAL=/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?$/;

export function validateWrappedEvmBalanceMap(value){
  if(!value||typeof value!=='object'||Array.isArray(value)||
      Object.keys(value).length!==Object.keys(WRAPPED_EVM_ASSETS).length)
    return false;
  return Object.entries(WRAPPED_EVM_ASSETS).every(([symbol,known])=>{
    const entry=value[symbol];
    return entry&&entry.asset===symbol&&entry.underlying===known.underlying&&
      entry.network===known.network&&entry.chain_id===known.chainId&&
      entry.contract===known.contract&&entry.decimals===18&&
      entry.native===false&&entry.wallet_funds_used_for_paper===false&&
      (entry.status==='unavailable'&&entry.balance===null||
       entry.status==='verified'&&typeof entry.balance==='string'&&
       entry.balance.length<=85&&DECIMAL.test(entry.balance));
  });
}


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
     !data.assets||typeof data.assets!=='object'||
     // Accept the old endpoint during rollout, but never accept a claimed
     // wrapped balance with the wrong chain, contract or decimal precision.
     (data.wrapped_assets!==undefined&&
      !validateWrappedEvmBalanceMap(data.wrapped_assets)))return false;

  return EVM_BALANCE_ASSETS.every(asset=>{
    const row=data.assets[asset];
    const isToken=asset==='LINK'||asset==='QNT'||asset==='UNI'||asset==='ARB';
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
