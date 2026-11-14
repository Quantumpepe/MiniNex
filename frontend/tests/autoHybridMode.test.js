import test from 'node:test';
import assert from 'node:assert/strict';
import {MODES as REPLAY_MODES,EXTRA_REPLAY_COINS,defaultReplayOption}
  from '../src/extraReplayRules.js';
import {validSharedStrategyProfile,applySharedStrategyToExtra,SHARED_STRATEGY_COINS}
  from '../src/sharedStrategyProfile.js';
import {validExtraPaperState,extraPaperCanStart} from '../src/extraPaperRules.js';

const profile=()=>({
  mode:'SHARED_STRATEGY_PREFERENCES',strategy_mode:'AUTO',
  horizon:'15m',revision:5,assets:[...SHARED_STRATEGY_COINS],
  ada_in_progress:true,independent_budgets_preserved:true,
  existing_sessions_unchanged:true,wallet_authorized:false,real_trading_enabled:false,
});

test('AUTO is the third strategy setting for eleven coins, not ADA',()=>{
  assert.deepEqual(REPLAY_MODES,['LONG','SPOT_SHORT','AUTO']);
  assert.equal(SHARED_STRATEGY_COINS.length,11);
  assert.equal(SHARED_STRATEGY_COINS.includes('ADA'),false);
  assert.equal(validSharedStrategyProfile(profile()),true);
});

test('applying AUTO once preserves each asset budget and stablecoin',()=>{
  const options=Object.fromEntries(EXTRA_REPLAY_COINS.map((asset,index)=>[
    asset,{...defaultReplayOption(),budget:String(100+index*100),
      stable:index%2===0?'USDC':'USDT'},
  ]));
  const hybrid=applySharedStrategyToExtra(options,profile());
  assert.equal(Object.keys(hybrid).length,9);
  for(const asset of EXTRA_REPLAY_COINS){
    assert.equal(hybrid[asset].mode,'AUTO');
    assert.equal(hybrid[asset].horizon,'15m');
    assert.equal(hybrid[asset].budget,options[asset].budget);
    assert.equal(hybrid[asset].stable,options[asset].stable);
    assert.equal(options[asset].mode,'LONG');
  }
});

test('AUTO budget form accepts mode, no live permission or auto-order declaration',()=>{
  const option={mode:'AUTO',horizon:'15m',stable:'USDC',budget:'500'};
  assert.equal(extraPaperCanStart(option),true);
  const unsafe=profile();
  unsafe.real_trading_enabled=true;
  assert.equal(validSharedStrategyProfile(unsafe),false);
  assert.throws(()=>applySharedStrategyToExtra({},unsafe));
});
