// Authoritative client boundary for private, opt-in extra-coin PAPER states.
import {EXTRA_REPLAY_COINS,validExtraReplayBudget} from './extraReplayRules.js';

const STATUSES=new Set(['NOT_STARTED','ACTIVE','PAUSED']);
const MODES=new Set(['LONG','SPOT_SHORT']);
const STABLES=new Set(['USDC','USDT']);
const HORIZONS=new Set(['5m','15m','1h','24h']);

export function validExtraPaperState(data,asset){
  if(!data||typeof data!=='object'||Array.isArray(data)||
     !EXTRA_REPLAY_COINS.includes(asset)||data.asset!==asset||
     data.paper_only!==true||data.simulation_only!==true||
     data.real_trading_enabled!==false||data.wallet_funds_used!==false||
     data.budget_isolated!==true||
     !STATUSES.has(data.status)||
     data.cost_model!=='GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES'||
     !Array.isArray(data.trades)||data.trades.length>30||
     !data.trades.every(trade=>trade&&trade.asset===asset&&
       trade.real_trade===false&&['PAPER_BUY','PAPER_SELL'].includes(trade.kind)))
    return false;
  if(data.status==='NOT_STARTED')
    return data.trades.length===0;
  if(!Number.isFinite(data.budget_usd)||data.budget_usd<100||
     data.budget_usd>5000||!MODES.has(data.strategy_mode)||
     !STABLES.has(data.settlement)||!HORIZONS.has(data.horizon)||
     !Number.isInteger(data.revision)||data.revision<0||
     !data.position||typeof data.position!=='object')return false;
  const fields=['stable_cash','coin_quantity','cost_basis','realized_net_profit'];
  if(!fields.every(key=>{
      const n=Number(data.position[key]);
      return data.position[key]!==undefined&&data.position[key]!==null&&
        typeof data.position[key]!=='boolean'&&Number.isFinite(n)&&
        (key==='realized_net_profit'||n>=0);
    }))return false;
  if(!Number.isInteger(data.position.trade_count)||
     data.position.trade_count<0||data.position.trade_count<data.trades.length||
     typeof data.pause_exit_pending!=='boolean')return false;
  return true;
}

export function extraPaperCanStart(options){
  return validExtraReplayBudget(options?.budget)&&
    MODES.has(options?.mode)&&HORIZONS.has(options?.horizon)&&
    STABLES.has(options?.stable);
}
