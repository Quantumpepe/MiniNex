import {EXTRA_REPLAY_COINS,MODES,HORIZONS,defaultReplayOption}
  from './extraReplayRules.js';

const STABLES=new Set(['USDC','USDT']);
const ACTIVE_MODES=new Set(MODES);
const TIMEFRAMES=new Set(HORIZONS);

export function validSavedExtraCoinPreferences(data){
  if(!data||typeof data!=='object'||Array.isArray(data)||
     data.mode!=='INDEPENDENT_COIN_PREFERENCES'||
     data.independent_budgets!==true||
     data.active_sessions_unchanged!==true||
     data.wallet_authorized!==false||
     data.live_trading_enabled!==false||
     !Array.isArray(data.assets)||
     data.assets.length!==EXTRA_REPLAY_COINS.length||
     !data.assets.every((asset,i)=>asset===EXTRA_REPLAY_COINS[i])||
     !data.settings||typeof data.settings!=='object'||Array.isArray(data.settings)||
     Object.keys(data.settings).length!==EXTRA_REPLAY_COINS.length)return false;
  return EXTRA_REPLAY_COINS.every(asset=>{
    const row=data.settings[asset];
    return row&&typeof row==='object'&&!Array.isArray(row)&&
      ACTIVE_MODES.has(row.mode)&&STABLES.has(row.stable)&&
      TIMEFRAMES.has(row.horizon)&&
      Number.isInteger(row.budget_usd)&&row.budget_usd>=100&&
      row.budget_usd<=5000&&Number.isInteger(row.revision)&&row.revision>=0;
  });
}

export function savedExtraOptions(data){
  if(!validSavedExtraCoinPreferences(data))throw Error('Invalid saved coin settings');
  return Object.fromEntries(EXTRA_REPLAY_COINS.map(asset=>[
    asset,{
      ...defaultReplayOption(),
      mode:data.settings[asset].mode,
      stable:data.settings[asset].stable,
      horizon:data.settings[asset].horizon,
      budget:String(data.settings[asset].budget_usd),
    },
  ]));
}

export function coinPreferenceChanged(options,saved){
  if(!options||!saved||!Number.isInteger(saved.revision))return false;
  return options.mode!==saved.mode||options.stable!==saved.stable||
    options.horizon!==saved.horizon||Number(options.budget)!==saved.budget_usd;
}
