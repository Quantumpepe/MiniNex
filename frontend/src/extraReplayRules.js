// Pure, testable safety/shape contract for on-demand historical paper tests.
export const EXTRA_REPLAY_COINS=Object.freeze([
  'BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI',
]);
export const MODES=['LONG','SPOT_SHORT'];
export const HORIZONS=['5m','15m','1h','24h'];
export const defaultReplayOption=()=>({mode:'LONG',horizon:'15m',budget:'500'});

export function validExtraReplayResult(value,asset,options) {
  if(!EXTRA_REPLAY_COINS.includes(asset)||
     !value||typeof value!=='object'||Array.isArray(value)||
     value.asset!==asset||value.mode!==options.mode||
     value.horizon!==options.horizon||
     value.paper_only!==true||value.hypothetical!==true||
     value.real_trading_enabled!==false||
     value.wallet_funds_used!==false||
     value.budget_isolated!==true||value.orders_executed!==0||
     !['SIMULATED','INSUFFICIENT_WINDOW','INSUFFICIENT_SAMPLES'].includes(value.status)||
     !Array.isArray(value.trades)||value.trades.length>30||
     !value.trades.every(row=>row&&row.asset===asset&&
       row.real_trade===false&&['PAPER_BUY','PAPER_SELL'].includes(row.kind))||
     !Number.isInteger(value.sample_count)||value.sample_count<0||
     value.sample_count>3600)return false;
  if(value.status!=='SIMULATED')
    return value.trades.length===0;
  return value.simulation_only===true&&
    value.historical_quotes_not_independently_verified===true&&
    value.cost_model==='GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES'&&
    typeof value.initial_usd==='number'&&
    value.initial_usd===Number(options.budget)&&
    ['estimated_net_return_pct','estimated_max_drawdown_pct',
     'estimated_final_liquidation_usd','estimated_buy_and_hold_return_pct',
     'estimated_strategy_minus_hold_pp','observed_minutes'].every(key=>
       Number.isFinite(value[key]))&&
    Number.isInteger(value.trade_count)&&value.trade_count>=0;
}


// A virtual budget is per-coin, never a real wallet allocation.
export function validExtraReplayBudget(value) {
  const amount=Number(value);
  return typeof value==='string'&&/^[1-9][0-9]{2,3}$/.test(value)&&
    Number.isInteger(amount)&&amount>=100&&amount<=5000;
}
