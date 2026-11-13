// Compact read-only comparison contract: never treat a missing price as PnL.
import {EXTRA_REPLAY_COINS} from './extraReplayRules.js';

const COST_BASIS='GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES';
const STATES=new Set(['NOT_STARTED','ACTIVE','PAUSED','UNAVAILABLE']);
const numericOptional=(value)=>value===null||(
  typeof value==='number'&&Number.isFinite(value));
const integer=(value)=>Number.isInteger(value)&&value>=0;
const ENTRY_REASONS=['DCA','REBOUND','BREAKOUT','TREND','OTHER'];
const EXIT_REASONS=['TRAILING','RISK','SPOT','PAUSE','OTHER'];
const ANALYSIS_STATES=new Set([
  'NOT_STARTED','UNAVAILABLE','INCOMPLETE_SAMPLE','NO_CLOSED_SELLS',
  'EARLY_SAMPLE','PRICE_UNAVAILABLE','OBSERVATIONS_AVAILABLE',
]);
const countsValid=(obj,names)=>obj&&typeof obj==='object'&&
  !Array.isArray(obj)&&Object.keys(obj).length===names.length&&
  names.every(key=>integer(obj[key])&&obj[key]<=30);
const sumCounts=(obj,names)=>names.reduce((sum,key)=>sum+obj[key],0);

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
       typeof row.recent_costs_complete!=='boolean'||
       !integer(row.recent_trade_sample_count)||
       row.recent_trade_sample_count>30||
       typeof row.recent_trade_sample_is_lifetime!=='boolean'||
       !countsValid(row.recent_entry_reason_counts,ENTRY_REASONS)||
       !countsValid(row.recent_exit_reason_counts,EXIT_REASONS)||
       typeof row.recent_sell_stats_complete!=='boolean'||
       !numericOptional(row.recent_sell_win_rate_pct)||
       !numericOptional(row.recent_average_sell_pnl_usd)||
       !numericOptional(row.recent_largest_loss_usd)||
       !numericOptional(row.market_change_24h_pct)||
       !ANALYSIS_STATES.has(row.analysis_status)||
       !(row.last_signal_summary===null||
         (typeof row.last_signal_summary==='string'&&
          row.last_signal_summary.length<=160)) ||
       (row.recent_sell_win_rate_pct!==null&&
        (row.recent_sell_win_rate_pct<0||row.recent_sell_win_rate_pct>100))||
       (row.recent_largest_loss_usd!==null&&row.recent_largest_loss_usd>=0)||
       (row.market_change_24h_pct!==null&&row.market_change_24h_pct< -100)||
       (sumCounts(row.recent_entry_reason_counts,ENTRY_REASONS)+
        sumCounts(row.recent_exit_reason_counts,EXIT_REASONS)>
        row.recent_trade_sample_count)||
       (!row.recent_sell_stats_complete&&
        (row.recent_sell_win_rate_pct!==null||
         row.recent_average_sell_pnl_usd!==null||
         row.recent_largest_loss_usd!==null)))return false;
    if(row.quote_valid!== (row.estimated_equity_usd!==null))return false;
    if(!row.quote_valid&&
       (row.estimated_total_pnl_usd!==null||row.estimated_net_return_pct!==null))
      return false;
    if(row.status==='NOT_STARTED'){
      return row.budget_usd===null&&row.trade_count===0&&
        row.estimated_equity_usd===null&&
        row.analysis_status==='NOT_STARTED';
    }
    if(row.status==='UNAVAILABLE')return row.quote_valid===false&&
      row.analysis_status==='UNAVAILABLE';
    if(row.recent_winning_sells+row.recent_losing_sells+
       row.recent_flat_sells>row.recent_closed_sells)return false;
    if(row.recent_buys+row.recent_closed_sells>30)return false;
    return typeof row.budget_usd==='number'&&
      row.budget_usd>=100&&row.budget_usd<=5000;
  });
}
