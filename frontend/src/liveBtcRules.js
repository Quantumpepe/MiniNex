import {usdUnits,validatedLiveEthDraft} from './liveEthRules.js';

// Same exact USD precision/caps as ETH, but a DIFFERENT stored BTC budget.
// A saved cap never authorizes a native BTC UTXO or a cross-chain swap.
export function validLiveBtcLimits(snapshot){
  if(snapshot?.asset!=='BTC'||
     snapshot?.network!=='bitcoin-mainnet'||
     snapshot?.mode!=='BTC_LIVE_LIMITS_PREPARATION_ONLY'||
     snapshot?.privy_btc_signer_verified!==false||
     snapshot?.btc_crosschain_execution_available!==false||
     snapshot?.native_utxo_spend_authorized!==false||
     snapshot?.paper_budget_used!==false||
     snapshot?.live_trading_enabled!==false||
     snapshot?.automated_signing_enabled!==false||
     snapshot?.trading_paused!==true||
     !Number.isSafeInteger(snapshot?.revision)||snapshot.revision<0)return false;
  const cap=usdUnits(snapshot.planned_capital_usd);
  const each=usdUnits(snapshot.max_order_usd);
  const daily=usdUnits(snapshot.max_daily_turnover_usd);
  if(cap===null||each===null||daily===null)return false;
  return (cap===0n&&each===0n&&daily===0n)||
    (cap>0n&&each>0n&&daily>0n&&each<=cap&&each<=daily);
}

export function validatedLiveBtcDraft(value){
  return validatedLiveEthDraft(value);
}
