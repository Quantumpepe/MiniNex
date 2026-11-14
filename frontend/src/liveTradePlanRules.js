// One quote request and one response contract for all eleven MiniNex coins.
// Real wallet balances are never inferred from PAPER budgets or preferences.
export const LIVE_PLAN_COINS=Object.freeze([
  'BTC','ETH','BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI',
]);
export const LIVE_SETTLEMENTS=Object.freeze({
  BTC:['USDC','USDT'],ETH:['USDC','USDT'],BNB:['USDT'],
  SOL:['USDC','USDT'],XRP:['USDC'],TRX:['USDT'],POL:['USDC'],
  AVAX:['USDC','USDT'],LINK:['USDC','USDT'],
  DOGE:['USDC','USDT'],UNI:['USDC','USDT'],
});
export const LIVE_NETWORKS=Object.freeze({
  BTC:'bitcoin-mainnet',ETH:'ethereum-mainnet',BNB:'bsc-mainnet',
  SOL:'solana-mainnet',XRP:'xrpl-mainnet',TRX:'tron-mainnet',
  POL:'polygon-pos-mainnet',AVAX:'avalanche-c-mainnet',
  LINK:'ethereum-mainnet',DOGE:'dogecoin-mainnet',UNI:'ethereum-mainnet',
});
const COIN_PRECISION=Object.freeze({
  BTC:8,ETH:18,BNB:8,SOL:8,XRP:6,TRX:6,POL:8,
  AVAX:8,LINK:8,DOGE:8,UNI:8,
});
const AMOUNT=/^(?:0|[1-9][0-9]*)(?:\.([0-9]+))?$/;
const POSITIVE=value=>typeof value==='string'&&value.length<=85&&
  AMOUNT.test(value)&&/[1-9]/.test(value);
const PROVIDER_LEVELS=Object.freeze({
  ETH:'WALLET_VERIFIED_UNSIGNED_SWAP',
  BTC:'WALLET_VERIFIED_CROSSCHAIN_INDICATIVE',
});

export function normalizedLiveStable(asset,stored){
  const allowed=LIVE_SETTLEMENTS[asset];
  return allowed ? (allowed.includes(stored)?stored:allowed[0]) : null;
}

export function createLivePlanRequest(asset,side,stable,amount){
  if(!LIVE_PLAN_COINS.includes(asset)||!['BUY','SELL'].includes(side)||
     !LIVE_SETTLEMENTS[asset].includes(stable)||!POSITIVE(amount)||
     amount.length>45)return null;
  const precision=side==='BUY'?6:COIN_PRECISION[asset];
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
  }else if(value.quote_level!=='VENUE_INDICATIVE_WALLET_NOT_VERIFIED'||
           value.wallet_funds_verified!==false||
           value.quote_provider!=='THORCHAIN_PUBLIC_MCP')return false;
  const minimum=value.minimum_output_amount;
  if(minimum!==null&&minimum!==undefined&&(!POSITIVE(minimum)||
     Number(minimum)>Number(value.estimated_output_amount)))return false;
  return true;
}
