import {COINS} from './coins.js';
import {EXTRA_REPLAY_COINS} from './extraReplayRules.js';

// Same eleven trading assets for every user; ADA stays "In Arbeit".
export const SHARED_STRATEGY_COINS=Object.freeze(
  COINS.filter(coin=>coin.inProgress!==true).map(coin=>coin.symbol));
const MODES=new Set(['LONG','SPOT_SHORT','AUTO']);
const HORIZONS=new Set(['5m','15m','1h','24h']);

export function validSharedStrategyProfile(data){
  return data&&typeof data==='object'&&!Array.isArray(data)&&
    data.mode==='SHARED_STRATEGY_PREFERENCES'&&
    MODES.has(data.strategy_mode)&&HORIZONS.has(data.horizon)&&
    Number.isSafeInteger(data.revision)&&data.revision>=0&&
    data.ada_in_progress===true&&
    data.independent_budgets_preserved===true&&
    data.existing_sessions_unchanged===true&&
    data.wallet_authorized===false&&data.real_trading_enabled===false&&
    Array.isArray(data.assets)&&data.assets.length===SHARED_STRATEGY_COINS.length&&
    data.assets.every((asset,i)=>asset===SHARED_STRATEGY_COINS[i]);
}

// Use one strategy but NEVER overwrite each coin's own amount/settlement.
export function applySharedStrategyToExtra(previous,profile){
  if(!validSharedStrategyProfile(profile))throw Error('Invalid shared strategy');
  return Object.fromEntries(EXTRA_REPLAY_COINS.map(asset=>[
    asset,{...previous[asset],mode:profile.strategy_mode,horizon:profile.horizon},
  ]));
}
