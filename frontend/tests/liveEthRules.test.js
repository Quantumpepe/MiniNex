import test from 'node:test';
import assert from 'node:assert/strict';
import {usdUnits,validLiveEthLimits,validSignerStatus,validatedLiveEthDraft} from '../src/liveEthRules.js';

const limits=()=>{
  return {
    asset:'ETH',chain_id:1,mode:'LIVE_LIMITS_PREPARATION_ONLY',
    planned_capital_usd:'500.000000',max_order_usd:'100.000000',
    max_daily_turnover_usd:'250.000000',revision:4,trading_paused:true,
    privy_grant_verified:false,live_trading_enabled:false,automated_signing_enabled:false,
  };
};
const status=()=>({
  asset:'ETH',chain_id:1,mode:'PRIVY_SIGNER_VERIFICATION_ONLY',
  execution_enabled:false,automated_signing_enabled:false,trading_approved:false,
  policy_contents_verified:false,wallet_delegated:false,signer_verified:false,
  policy_reference_verified:false,
});

test('USD limits use exact integer base units; reject invalid numeric forms',()=>{
  assert.equal(usdUnits('100.000001'),100000001n);
  assert.equal(usdUnits('0'),0n);
  assert.equal(usdUnits('9999999.999999'),9999999999999n);
  for(const bad of [
    null,0,100,false,'-1','NaN','Infinity','1e3','01',' 1','1.0000001',
    '10000000','1,000','1.2.3','', '0x12'
  ])assert.equal(usdUnits(bad),null);
});

test('ETH limit snapshots are safe only when LIVE signer and order access are off',()=>{
  assert.equal(validLiveEthLimits(limits()),true);
  const denied=[
    {asset:'BTC'}, {chain_id:56},
    {live_trading_enabled:true}, {automated_signing_enabled:true},
    {privy_grant_verified:true}, {mode:'LIVE_ACTIVE'},
    {max_order_usd:'501'}, {max_daily_turnover_usd:'50'},
    {planned_capital_usd:'not-a-number'}, {revision:-1},
  ];
  for(const patch of denied)assert.equal(validLiveEthLimits({...limits(),...patch}),false);
});

test('exact LIVE spending draft must respect caps',()=>{
  const draft={
    planned_capital_usd:'150.10',max_order_usd:'60',max_daily_turnover_usd:'70',
  };
  assert.deepEqual(validatedLiveEthDraft(draft),draft);
  assert.deepEqual(validatedLiveEthDraft({
    planned_capital_usd:'0',max_order_usd:'0',max_daily_turnover_usd:'0'
  }),{planned_capital_usd:'0',max_order_usd:'0',max_daily_turnover_usd:'0'});
  assert.equal(validatedLiveEthDraft({...draft,max_order_usd:'180'}),null);
  assert.equal(validatedLiveEthDraft({...draft,max_daily_turnover_usd:'10'}),null);
  assert.equal(validatedLiveEthDraft({...draft,planned_capital_usd:'0'}),null);
  assert.equal(validatedLiveEthDraft({...draft,planned_capital_usd:'1e5'}),null);
});

test('Privy signer status must never be mistaken for active trading',()=>{
  assert.equal(validSignerStatus(status()),true);
  assert.equal(validSignerStatus({...status(),signer_verified:true,policy_reference_verified:true}),true);
  for(const patch of [
    {mode:'AUTHORIZED'}, {execution_enabled:true},
    {automated_signing_enabled:true}, {trading_approved:true},
    {policy_contents_verified:true}, {chain_id:56}, {asset:'BTC'}
  ])assert.equal(validSignerStatus({...status(),...patch}),false);
});
