// One backward-compatible quote response contract for all twelve MiniNex coins.
// Real wallet balances are never inferred from PAPER budgets or preferences.
export const LIVE_PLAN_COINS=Object.freeze([
  'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ARB',
]);
export const LIVE_SETTLEMENTS=Object.freeze({
  BTC:['USDC','USDT'],ETH:['USDC','USDT'],BNB:['USDT'],
  SOL:['USDC','USDT'],XRP:['USDC'],TRX:['USDT'],POL:['USDC'],
  AVAX:['USDC'],LINK:['USDC','USDT'],
  DOGE:['USDC','USDT'],UNI:['USDC','USDT'],ARB:['USDC','USDT'],
});
export const LIVE_NETWORKS=Object.freeze({
  BTC:'bitcoin-mainnet',ETH:'ethereum-mainnet',BNB:'bsc-mainnet',
  SOL:'solana-mainnet',XRP:'xrpl-mainnet',TRX:'tron-mainnet',
  POL:'polygon-pos-mainnet',AVAX:'avalanche-c-mainnet',
  LINK:'ethereum-mainnet',DOGE:'dogecoin-mainnet',UNI:'ethereum-mainnet',
  ARB:'arbitrum-one-mainnet',
});
const COIN_PRECISION=Object.freeze({
  BTC:8,ETH:18,BNB:18,SOL:9,XRP:6,TRX:6,POL:18,
  AVAX:18,LINK:18,DOGE:8,UNI:18,ARB:18,
});
const LIVE_EVM_CHAINS=Object.freeze({
  ETH:1,BNB:56,POL:137,AVAX:43114,LINK:1,UNI:1,ARB:42161,
});
const AMOUNT=/^(?:0|[1-9][0-9]*)(?:\.([0-9]+))?$/;
const POSITIVE=value=>typeof value==='string'&&value.length<=85&&
  AMOUNT.test(value)&&/[1-9]/.test(value);
const PROVIDER_LEVELS=Object.freeze({
  ETH:'WALLET_VERIFIED_UNSIGNED_SWAP',
  BTC:'WALLET_VERIFIED_CROSSCHAIN_INDICATIVE',
  ERC20:'WALLET_VERIFIED_UNSIGNED_ERC20_SWAP',
  NATIVE_EVM:'WALLET_VERIFIED_UNSIGNED_NATIVE_EVM_SWAP',
  NATIVE_OWNER:'OWNER_WALLET_BALANCE_VERIFIED_NATIVE_INDICATIVE',
});

export function normalizedLiveStable(asset,stored){
  const allowed=LIVE_SETTLEMENTS[asset];
  return allowed ? (allowed.includes(stored)?stored:allowed[0]) : null;
}

export function createLivePlanRequest(asset,side,stable,amount){
  if(!LIVE_PLAN_COINS.includes(asset)||!['BUY','SELL'].includes(side)||
     !LIVE_SETTLEMENTS[asset].includes(stable)||!POSITIVE(amount)||
     amount.length>45)return null;
  // BNB-chain USDT has 18 decimals, unlike 6-decimal Ethereum USDT.
  const precision=side==='BUY'?(asset==='BNB'?18:6):COIN_PRECISION[asset];
  const fractions=amount.split('.')[1]||'';
  if(fractions.length>precision)return null;
  return {
    asset,side,stable,amount,
    inputAsset:side==='BUY'?stable:asset,
    outputAsset:side==='BUY'?asset:stable,
    path:'/api/live/trade-plan/'+asset,
    body:{side,stable,amount},
  };
}

export function validLiveTradePlan(value,request){
  if(!request||!value||typeof value!=='object'||Array.isArray(value)||
     value.mode!=='UNIFIED_ELEVEN_COIN_TRADE_PLAN'||
     value.asset!==request.asset||value.side!==request.side||
     value.stable!==request.stable||value.input_amount!==request.amount||
     value.source_asset!==request.inputAsset||
     value.destination_asset!==request.outputAsset||
     value.network!==LIVE_NETWORKS[request.asset]||
     (LIVE_EVM_CHAINS[request.asset]!==undefined &&
       value.chain_id!==LIVE_EVM_CHAINS[request.asset])||
     value.route_plan_only!==true||
     value.real_order_created!==false||
     value.wallet_signed!==false||value.funds_transferred!==false||
     value.can_submit!==false||value.execution_enabled!==false||
     !POSITIVE(value.estimated_output_amount))return false;
  if(request.asset==='ETH'){
    if(value.quote_level!==PROVIDER_LEVELS.ETH||
       value.wallet_funds_verified!==true||
       value.quote_provider!=='UNISWAP_ETHEREUM')return false;
  }else if(request.asset==='BTC'){
    if(value.quote_level!==PROVIDER_LEVELS.BTC||
       value.wallet_funds_verified!==true||
       value.quote_provider!=='THORCHAIN_PUBLIC_MCP')return false;
  }else if(['LINK','UNI','ARB'].includes(request.asset)){
    if(value.quote_level!==PROVIDER_LEVELS.ERC20||
       value.wallet_funds_verified!==true||
       value.quote_provider!==(request.asset==='ARB'
         ? 'UNISWAP_V3_CHAIN_SPECIFIC' : 'UNISWAP_ETHEREUM_ERC20')||
       (request.asset==='ARB'&&value.native_wrap_unwrap_not_yet_settled!==false))return false;
  }else if(['BNB','POL','AVAX'].includes(request.asset)){
    if(value.quote_level!==PROVIDER_LEVELS.NATIVE_EVM||
       value.wallet_funds_verified!==true||
       value.quote_provider!=='UNISWAP_V3_CHAIN_SPECIFIC'||
       value.native_wrap_unwrap_not_yet_settled!==true)return false;
  }else if(['SOL','XRP'].includes(request.asset)){
    if(value.quote_level!==PROVIDER_LEVELS.NATIVE_OWNER||
       value.wallet_funds_verified!==true||
       value.source_chain_balance_verified!==true||
       value.network_fee_spendability_verified!==false||
       value.destination_settlement_finalized!==false||
       value.quote_provider!=='THORCHAIN_PUBLIC_MCP')return false;
  }else if(value.quote_level!=='VENUE_INDICATIVE_WALLET_NOT_VERIFIED'||
           value.wallet_funds_verified!==false||
           value.quote_provider!=='THORCHAIN_PUBLIC_MCP')return false;
  const minimum=value.minimum_output_amount;
  if(minimum!==null&&minimum!==undefined&&(!POSITIVE(minimum)||
     Number(minimum)>Number(value.estimated_output_amount)))return false;
  return true;
}
