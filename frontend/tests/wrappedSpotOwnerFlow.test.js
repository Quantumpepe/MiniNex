import test from 'node:test';
import assert from 'node:assert/strict';
import {readWrappedOwnerSettings,saveWrappedOwnerSettings,validWrappedSettings,
  validWrappedGrantAudit,validWrappedLiveSession} from '../src/wrappedSpotOwnerFlow.js';

const limits=(budget,revision)=>({asset:'BTC',network:'bitcoin-mainnet',
  mode:'BTC_LIVE_LIMITS_PREPARATION_ONLY',planned_capital_usd:budget,
  max_order_usd:budget,max_daily_turnover_usd:budget,revision,
  trading_paused:true,privy_btc_signer_verified:false,btc_crosschain_execution_available:false,
  native_utxo_spend_authorized:false,paper_budget_used:false,live_trading_enabled:false,
  automated_signing_enabled:false});
const prefs=(stable,revision)=>({mode:'PREFERENCES_ONLY',signing_authorized:false,
  settings:{BTC:{stable,mode:'LONG',horizon:'15m',revision}}});
test('BTC Start Trade saves the exact USD capital and stable before any grant',async()=>{
  let stored=limits('10',1),preferences=prefs('USDC',1);
  const calls=[];
  const request=async(path,method='GET',body)=>{
    calls.push([path,method]);
    if(path==='/api/live/btc/limits'){
      if(method==='PUT'){
        assert.equal(body.expected_revision,1);
        assert.equal(body.planned_capital_usd,'235.50');
        stored=limits('235.50',2);
      }
      return stored;
    }
    if(path==='/api/trade-preferences/BTC'){
      assert.equal(body.stable,'USDT');
      preferences=prefs('USDT',2);
    }
    return preferences;
  };
  const original=await readWrappedOwnerSettings('BTC',request);
  const result=await saveWrappedOwnerSettings('BTC',{...original.settings,
    budget:'235.50',stable:'USDT'},original,request);
  assert.equal(result.settings.budget,'235.50');
  assert.equal(result.settings.stable,'USDT');
  assert.equal(result.budgetRevision,2);
  assert.equal(calls.filter(c=>c[1]==='PUT').length,2);
});
test('BTC and XRP reject zero and nonnumeric trading capital',()=>{
  for(const asset of ['BTC','XRP','SOL']){
    const settings={stable:'USDC',mode:'LONG',horizon:'15m'};
    assert.equal(validWrappedSettings(asset,{...settings,budget:'0'}),false);
    assert.equal(validWrappedSettings(asset,{...settings,budget:'100'}),true);
    assert.equal(validWrappedSettings(asset,{...settings,budget:'garbage'}),false);
  }
});
test('grant audit cannot be mistaken for a live session',()=>{
  const report={mode:'EVM_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT',
    grant_source:'PRIVY_CENTRAL_ONE_SIGNER',asset:'BTC',chain_id:1,
    current_user_budget_revision:2,owner_wallet_current:true,
    exact_remote_policy_rules_verified:true,privy_additional_signer_scoped:true,
    backend_quorum_current:true,signed_coin_budget_intent_in_ledger:true,
    can_broadcast:false,live_session_verified:false,live_execution_enabled_by_this_audit:false};
  assert.equal(validWrappedGrantAudit(report,'BTC',2),true);
  assert.equal(validWrappedGrantAudit({...report,live_session_verified:true},'BTC',2),false);
  assert.equal(validWrappedGrantAudit(report,'BTC',3),false);
});

test('BTC and XRP use real Ethereum session status, never native status or a grant as a session',()=>{
  for(const asset of ['BTC','XRP','SOL']){
    const session={asset,mode:'OWNER_EVM_LIVE_SESSION_CONTROL',chain_id:1,status:'ACTIVE',
      worker_attached:true,original_blockchain_swaps_sent:2,original_blockchain_swaps_finalized:1};
    assert.equal(validWrappedLiveSession(session,asset),true);
    assert.equal(validWrappedLiveSession({...session,chain_id:56},asset),false);
    assert.equal(validWrappedLiveSession({...session,mode:'OWNER_NATIVE_LIVE_SESSION_CONTROL'},asset),false);
    assert.equal(validWrappedLiveSession({...session,original_blockchain_swaps_finalized:3},asset),false);
  }
});
