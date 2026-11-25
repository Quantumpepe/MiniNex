// One explicit owner Start Trade action: persist exact settings, authorize
// the existing Ethereum signer, and independently audit the approved scope.
// No order, session or native wallet send is created by this grant flow.
import {authorizeCentralEvmWalletCoin,centralWalletHasCoin} from './centralEvmWalletFlow.js';
import {validLiveBtcLimits} from './liveBtcRules.js';
import {usdUnits,validatedLiveEthDraft} from './liveEthRules.js';
import {validSavedExtraCoinPreferences} from './extraCoinPreferenceRules.js';

const STABLES=['USDC','USDT'], MODES=['LONG','SPOT_SHORT','AUTO'];
const HORIZONS=['5m','15m','1h','24h'];
const coins=['BTC','XRP','SOL'];
const validSettings=x=>x&&STABLES.includes(x.stable)&&MODES.includes(x.mode)&&
  HORIZONS.includes(x.horizon)&&Number.isSafeInteger(x.revision)&&x.revision>=0;

export function validWrappedSettings(asset,settings){
  if(!coins.includes(asset)||!validSettings({...settings,revision:0}))return false;
  if(asset==='BTC'){
    const value=usdUnits(settings.budget);
    return value!==null&&value>0n;
  }
  return typeof settings.budget==='string'&&/^[1-9][0-9]*$/.test(settings.budget)&&
    Number.isSafeInteger(Number(settings.budget));
}

export async function readWrappedOwnerSettings(asset,request){
  if(asset==='BTC'){
    const [limits,prefs]=await Promise.all([
      request('/api/live/btc/limits'),request('/api/trade-preferences'),
    ]);
    const coin=prefs?.settings?.BTC;
    if(!validLiveBtcLimits(limits)||prefs?.mode!=='PREFERENCES_ONLY'||
      prefs?.signing_authorized!==false||!validSettings(coin))
      throw Error('BTC saved settings unavailable');
    return {settings:{budget:limits.planned_capital_usd,stable:coin.stable,
      mode:coin.mode,horizon:coin.horizon},budgetRevision:limits.revision,
      preferenceRevision:coin.revision};
  }
  if(!['XRP','SOL'].includes(asset))throw Error('Unsupported coin');
  const prefs=await request('/api/coin-preferences');
  if(!validSavedExtraCoinPreferences(prefs))throw Error(asset+' saved settings unavailable');
  const coin=prefs.settings[asset];
  return {settings:{budget:String(coin.budget_usd),stable:coin.stable,
    mode:coin.mode,horizon:coin.horizon},budgetRevision:coin.revision,
    preferenceRevision:coin.revision};
}

export async function saveWrappedOwnerSettings(asset,settings,original,request){
  if(!validWrappedSettings(asset,settings)||!original||
     !Number.isSafeInteger(original.budgetRevision)||
     !Number.isSafeInteger(original.preferenceRevision))throw Error('Save valid trading capital first');
  if(asset==='BTC'){
    const {budget,stable,mode,horizon}=settings;
    if(original.budgetRevision===0||
       usdUnits(budget)!==usdUnits(original.settings.budget)){
      const payload=validatedLiveEthDraft({planned_capital_usd:budget});
      const saved=await request('/api/live/btc/limits','PUT',{
        ...payload,expected_revision:original.budgetRevision,
      });
      if(!validLiveBtcLimits(saved)||usdUnits(saved.planned_capital_usd)!==usdUnits(budget)||
        saved.revision<=original.budgetRevision)throw Error('BTC capital was not saved');
    }
    if(original.preferenceRevision===0||
       ['stable','mode','horizon'].some(k=>settings[k]!==original.settings[k])){
      const result=await request('/api/trade-preferences/BTC','PUT',{
        stable,mode,horizon,expected_revision:original.preferenceRevision,
      });
      const saved=result?.settings?.BTC;
      if(!validSettings(saved)||saved.revision<=original.preferenceRevision||
         saved.stable!==stable||saved.mode!==mode||saved.horizon!==horizon)
        throw Error('BTC settings were not saved');
    }
  }else if(original.preferenceRevision===0||
     ['budget','stable','mode','horizon'].some(k=>settings[k]!==original.settings[k])){
    const result=await request('/api/coin-preferences/'+asset,'PUT',{
      budget_usd:Number(settings.budget),stable:settings.stable,mode:settings.mode,
      horizon:settings.horizon,expected_revision:original.preferenceRevision,
    });
    if(!validSavedExtraCoinPreferences(result)||
       result.settings[asset].revision<=original.preferenceRevision)
      throw Error(asset+' capital was not saved');
  }
  // Read back the exact version used by the server-side policy constructor.
  const current=await readWrappedOwnerSettings(asset,request);
  if(current.budgetRevision<1||current.preferenceRevision<1||
     (asset==='BTC'?usdUnits(current.settings.budget)!==usdUnits(settings.budget):
       current.settings.budget!==settings.budget)||
     ['stable','mode','horizon'].some(k=>current.settings[k]!==settings[k]))
    throw Error('Current saved coin settings changed');
  return current;
}

export function validWrappedGrantAudit(report,asset,revision){
  return !!report&&report.mode==='EVM_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT'&&
    report.grant_source==='PRIVY_CENTRAL_ONE_SIGNER'&&report.asset===asset&&
    report.chain_id===1&&report.current_user_budget_revision===revision&&
    ['owner_wallet_current','exact_remote_policy_rules_verified',
      'privy_additional_signer_scoped','backend_quorum_current',
      'signed_coin_budget_intent_in_ledger'].every(k=>report[k]===true)&&
    report.can_broadcast===false&&report.live_session_verified===false&&
    report.live_execution_enabled_by_this_audit===false;
}

export async function authorizeWrappedOwnerTrade({asset,address,settings,original,
  request,addSigners,signMessage}){
  const saved=await saveWrappedOwnerSettings(asset,settings,original,request);
  const grant=await authorizeCentralEvmWalletCoin({asset,stable:saved.settings.stable,
    address,request,addSigners,signMessage});
  if(!centralWalletHasCoin(grant,asset))throw Error('Coin grant not verified');
  const audit=await request('/api/live/evm/wallet-policy/wrapped/'+asset+'/audit');
  if(!validWrappedGrantAudit(audit,asset,saved.budgetRevision)||
     audit.remote_verified_policy_sha256!==grant.verified_policy_sha256)
    throw Error('Current Privy authorization differs from saved capital');
  return {saved,grant,audit};
}

export function validWrappedLiveSession(data,asset){
  return ['BTC','XRP','SOL'].includes(asset)&&!!data&&data.asset===asset&&
    data.mode==='OWNER_EVM_LIVE_SESSION_CONTROL'&&
    ['NOT_STARTED','ACTIVE','PAUSED','EXIT_PENDING','STOPPED'].includes(data.status)&&
    (data.chain_id===1||data.chain_id===null)&&typeof data.worker_attached==='boolean'&&
    Number.isSafeInteger(data.original_blockchain_swaps_sent)&&data.original_blockchain_swaps_sent>=0&&
    Number.isSafeInteger(data.original_blockchain_swaps_finalized)&&
    data.original_blockchain_swaps_finalized>=0&&
    data.original_blockchain_swaps_finalized<=data.original_blockchain_swaps_sent;
}
export async function startWrappedOwnerTrade(options){
  const grant=await authorizeWrappedOwnerTrade(options);
  const started=await options.request('/api/live/evm/wallet-policy/auto-session/'+options.asset+'/start','POST');
  if(started?.mode!=='OWNER_EVM_LIVE_SESSION_CONTROL'||started.asset!==options.asset||
     started.chain_id!==1||started.status!=='ACTIVE'||started.started_by_owner!==true||
     started.new_transactions_sent!==0)throw Error('Original trading session not confirmed');
  const session=await options.request('/api/live/evm/auto-session/'+options.asset);
  if(!validWrappedLiveSession(session,options.asset)||session.status!=='ACTIVE')
    throw Error('Original trading session status unavailable');
  return {...grant,session};
}
