import test from 'node:test';
import assert from 'node:assert/strict';
import {usdUnits,validLiveEthLimits,validSignerStatus,validatedLiveEthDraft,validWalletSignerAbsence,validEthPolicyLifecycle,validScopedGrantOffer} from '../src/liveEthRules.js';

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


const walletAddress = '0x'+'a'.repeat(40);
const remoteAbsence = ()=>({
  mode:'PRIVY_SIGNER_ABSENCE_REMOTE_READ_ONLY',
  asset:'ETH',chain_id:1,
  verified_wallet_address:walletAddress,
  wallet_identity_verified:true,
  wallet_owned_by_authenticated_user:true,
  remote_wallet_metadata_verified:true,
  additional_signers_field_verified:true,
  signer_absence_verified:true,
  privy_remote_signer_removed_by_backend:false,
  privy_policy_deleted:false,
  execution_enabled:false,can_sign:false,can_submit:false,
});

test('confirmed Privy signer absence requires exact verified wallet',()=>{
  assert.equal(validWalletSignerAbsence(remoteAbsence(),walletAddress),true);
  assert.equal(validWalletSignerAbsence(remoteAbsence(),walletAddress.toUpperCase().replace('0X','0x')),true);
  assert.equal(validWalletSignerAbsence(remoteAbsence(),'0x'+'b'.repeat(40)),false);
  for(const invalid of [null,'',0,undefined,'abcdef']) {
    assert.equal(validWalletSignerAbsence(remoteAbsence(),invalid),false);
  }
});

test('a successful SDK removal without independent remote readback is never proof',()=>{
  const original=remoteAbsence();
  for(const patch of [
    {mode:'USER_CONFIRMED'}, {chain_id:56}, {asset:'BTC'},
    {wallet_identity_verified:false},
    {wallet_owned_by_authenticated_user:false},
    {remote_wallet_metadata_verified:false},
    {additional_signers_field_verified:false},
    {signer_absence_verified:false},
    {signer_absence_verified:null},
    {verified_wallet_address:null},
    {verified_wallet_address:'0x'+'b'.repeat(40)},
    {privy_policy_deleted:true},
    {privy_remote_signer_removed_by_backend:true},
    {execution_enabled:true},{can_sign:true},{can_submit:true},
  ])assert.equal(validWalletSignerAbsence({...original,...patch},walletAddress),false);
  assert.equal(validWalletSignerAbsence({walletRemovedBySdk:true},walletAddress),false);
  assert.equal(validWalletSignerAbsence(null,walletAddress),false);
});


const policyLifecycle=()=>({
  mode:'ETH_PRIVY_POLICY_DURABLE_PREPARATION_ONLY',
  asset:'ETH',chain_id:1,status:'NOT_PREPARED',
  execution_enabled:false,can_sign:false,can_submit:false,
  policy_created_in_privy:false,
});

test('ETH policy state stays inert in every preparation and grant state',()=>{
  for(const status of [
    'NOT_PREPARED','PREPARED','SUBMITTING','UNCERTAIN',
    'REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED',
    'REVOKE_REQUESTED','REVOKED',
  ])assert.equal(validEthPolicyLifecycle({...policyLifecycle(),status}),true);
  for(const patch of [
    {asset:'BTC'},{chain_id:137},{status:'ENABLE_LIVE'},
    {can_sign:true},{can_submit:true},{execution_enabled:true},
    {policy_created_in_privy:true},{mode:'LIVE_ORDER'},
  ])assert.equal(validEthPolicyLifecycle({...policyLifecycle(),...patch}),false);
});

const scopedGrant=()=>({
  mode:'ETH_PRIVY_SCOPED_GRANT_REVIEW_ONLY',
  asset:'ETH',chain_id:1,
  verified_wallet_address:'0x'+'a'.repeat(40),
  policy_id:'policy_scoped_user_2324',
  execution_signer_id:'signer_scoped_backend_2324',
  policy_verified:true,wallet_owned_by_user:true,
  unexpected_signers:false,user_privy_grant_verified:false,
  grant_offer_enabled:true,can_request_user_grant:true,
  cumulative_spend_enforced_by_policy:false,
  production_trading_enabled:false,
  can_sign:false,can_submit:false,execution_enabled:false,
});

test('explicit Privy signer grant offer must be independent and wallet-bound',()=>{
  const address='0x'+'a'.repeat(40);
  assert.equal(validScopedGrantOffer(scopedGrant(),address),true);
  for(const patch of [
    {mode:'PRIVY_UNRESTRICTED'}, {asset:'BTC'}, {chain_id:56},
    {verified_wallet_address:'0x'+'b'.repeat(40)},
    {policy_verified:false},{wallet_owned_by_user:false},
    {unexpected_signers:true},{user_privy_grant_verified:true},
    {grant_offer_enabled:false},{can_request_user_grant:false},
    {execution_enabled:true},{production_trading_enabled:true},
    {can_submit:true},{can_sign:true},
    {policy_id:''}, {execution_signer_id:'invalid id'},
    {policy_id:'signer_scoped_backend_2324'},
    {cumulative_spend_enforced_by_policy:true},
  ])assert.equal(validScopedGrantOffer({...scopedGrant(),...patch},address),false);
  assert.equal(validScopedGrantOffer(scopedGrant(),'0x'+'b'.repeat(40)),false);
  assert.equal(validScopedGrantOffer(null,address),false);
});
