// Compact read-only comparison contract: never treat a missing price as PnL.
import {EXTRA_REPLAY_COINS} from './extraReplayRules.js';

const COST_BASIS='GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES';
const STATES=new Set(['NOT_STARTED','ACTIVE','PAUSED','UNAVAILABLE']);
const numericOptional=(value)=>value===null||(
  typeof value==='number'&&Number.isFinite(value));
const integer=(value)=>Number.isInteger(value)&&value>=0;

export function validExtraPaperComparison(data){
  if(!data||typeof data!=='object'||Array.isArray(data)||
     data.paper_only!==true||data.simulation_only!==true||
     data.wallet_funds_used!==false||data.real_trading_enabled!==false||
     data.parameters_unchanged!==true||data.recent_window_limit!==30||
     !Array.isArray(data.assets)||data.assets.length!==EXTRA_REPLAY_COINS.length)
    return false;
  return data.assets.every((row,index)=>{
    if(!row||typeof row!=='object'||Array.isArray(row)||
       row.asset!==EXTRA_REPLAY_COINS[index]||
       !STATES.has(row.status)||row.paper_only!==true||
       row.simulation_only!==true||row.wallet_funds_used!==false||
       row.real_trading_enabled!==false||row.budget_isolated!==true||
       row.parameters_unchanged!==true||row.recent_window_limit!==30||
       row.cost_model!==COST_BASIS||typeof row.quote_valid!=='boolean'||
       !integer(row.trade_count)||
       !['recent_buys','recent_closed_sells','recent_winning_sells',
         'recent_losing_sells','recent_flat_sells'].every(k=>integer(row[k]))||
       !['budget_usd','estimated_equity_usd','estimated_total_pnl_usd',
         'estimated_net_return_pct','cumulative_realized_pnl_usd',
         'recent_realized_pnl_usd','recent_estimated_costs_usd']
         .every(k=>numericOptional(row[k]))||
       typeof row.recent_costs_complete!=='boolean')return false;
    if(row.quote_valid!== (row.estimated_equity_usd!==null))return false;
    if(!row.quote_valid&&
       (row.estimated_total_pnl_usd!==null||row.estimated_net_return_pct!==null))
      return false;
    if(row.status==='NOT_STARTED'){
      return row.budget_usd===null&&row.trade_count===0&&
        row.estimated_equity_usd===null;
    }
    if(row.status==='UNAVAILABLE')return row.quote_valid===false;
    if(row.recent_winning_sells+row.recent_losing_sells+
       row.recent_flat_sells>row.recent_closed_sells)return false;
    if(row.recent_buys+row.recent_closed_sells>30)return false;
    return typeof row.budget_usd==='number'&&
      row.budget_usd>=100&&row.budget_usd<=5000;
  });
}
