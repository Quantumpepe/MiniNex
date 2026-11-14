import test from 'node:test';
import assert from 'node:assert/strict';
import {SHARED_STRATEGY_COINS,validSharedStrategyProfile,applySharedStrategyToExtra}
  from '../src/sharedStrategyProfile.js';
import {EXTRA_REPLAY_COINS,defaultReplayOption} from '../src/extraReplayRules.js';

const profile=()=>({
  mode:'SHARED_STRATEGY_PREFERENCES',
  strategy_mode:'SPOT_SHORT',horizon:'1h',revision:3,
  assets:[...SHARED_STRATEGY_COINS],
  ada_in_progress:true,independent_budgets_preserved:true,
  existing_sessions_unchanged:true,wallet_authorized:false,
  real_trading_enabled:false,
});
test('global strategy applies to exactly eleven coins, ADA stays In Arbeit',()=>{
  assert.equal(SHARED_STRATEGY_COINS.length,11);
  assert.equal(SHARED_STRATEGY_COINS.includes('ADA'),false);
  assert.equal(validSharedStrategyProfile(profile()),true);
});
test('common mode and horizon never overwrite isolated coin budgets or settlement',()=>{
  const options=Object.fromEntries(EXTRA_REPLAY_COINS.map((coin,i)=>[
    coin,{...defaultReplayOption(),budget:String(100+i*100),
          stable:i%2?'USDT':'USDC'},
  ]));
  const changed=applySharedStrategyToExtra(options,profile());
  assert.equal(Object.keys(changed).length,9);
  for(const coin of EXTRA_REPLAY_COINS){
    assert.equal(changed[coin].budget,options[coin].budget);
    assert.equal(changed[coin].stable,options[coin].stable);
    assert.equal(changed[coin].mode,'SPOT_SHORT');
    assert.equal(changed[coin].horizon,'1h');
    assert.equal(options[coin].mode,'LONG');
  }
});
test('wrong asset order, live claim or missing revision cannot apply',()=>{
  const wrong=profile();wrong.assets.reverse();
  assert.equal(validSharedStrategyProfile(wrong),false);
  const unsafe=profile();unsafe.wallet_authorized=true;
  assert.equal(validSharedStrategyProfile(unsafe),false);
  const stale=profile();stale.revision='3';
  assert.equal(validSharedStrategyProfile(stale),false);
  assert.throws(()=>applySharedStrategyToExtra({},unsafe));
});
