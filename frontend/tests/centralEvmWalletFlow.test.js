import test from 'node:test';
import assert from 'node:assert/strict';
import {
  authorizeCentralEvmWalletCoin,centralWalletHasCoin,validCentralWalletStatus,
} from '../src/centralEvmWalletFlow.js';

const wallet='0x'+'a'.repeat(40);
const treasury='0x'+'b'.repeat(40);
const signer='signer_12345678';
const policyId='policy_12345678';
const digest='1'.repeat(64);
const newDigest='2'.repeat(64);
const nonce='c'.repeat(64);
const scopeStatus=(coins,status='REMOTE_VERIFIED_READONLY')=>({
  mode:'MININEX_ORIGINAL_EVM_WALLET_POLICY_READONLY_STATUS',
  status,scope_coins:coins,
  can_submit:false,auto_execution_enabled:false,
  privy_wallet_grant_changed:false,new_policy_created:false,
  legacy_reconciliation_required:false,current_user_budgets_verified:true,
  one_scoped_signer_verified:true,remote_policy_exactly_verified:true,
  backend_quorum_current:true,policy_owner_quorum_current:true,
});
const options=(request,extra={})=>({
  asset:'QNT',stable:'USDC',address:wallet,request,
  signMessage:async()=>{throw Error('Unexpected signMessage');},
  addSigners:async()=>{throw Error('Unexpected addSigners');},
  ...extra,
});
const draft=()=>({
  mode:'ORIGINAL_EVM_ONE_SIGNER_POLICY_DRAFT_ONLY',
  status:'DRAFT',asset:'QNT',wallet_address:wallet,
  shared_signer_id:signer,policy_sha256:digest,
  policy_created_in_privy:false,
  wallet_additional_signer_granted:false,
  live_trading_enabled:false,can_submit:false,
});
const published=()=>({
  status:'REMOTE_VERIFIED_UNATTACHED',asset:'QNT',
  policy_id:policyId,execution_signer_id:signer,
  policy_sha256:digest,wallet_additional_signer_attached:false,
  owner_must_approve_add_signers_once:true,can_submit:false,
});
const offer=()=>({
  mode:'ORIGINAL_EVM_ONE_SIGNER_GRANT_OFFER',
  wallet_address:wallet,asset:'QNT',stable:'USDC',
  execution_signer_id:signer,policy_id:policyId,
  policy_sha256:digest,owner_add_signer_required:true,
  original_wallet_approved:false,live_trading_enabled:false,
  can_submit:false,
  fee_scope:{
    treasury_wallet:treasury,fee_max_atomic:'1000000',
    performance_fee_percent:5,fee_basis:'REALIZED_NET_GAIN_ABOVE_HWM',
  },
});
const confirmed=()=>({
  state:'GRANT_VERIFIED',owner_signer_grant_verified:true,
  additional_coin_grants_created:0,
  live_auto_session_started:false,can_submit:false,
});
const challenge=()=>({
  mode:'OWNER_EVM_WALLET_POLICY_INTENT_ONLY',
  status:'PENDING',asset:'QNT',nonce,
  previous_policy_sha256:digest,
  target_policy_sha256:newDigest,
  scope_coins:['LINK','QNT'],
  additional_privy_signer_required:false,
  one_user_message_signature_required:true,
  policy_updated_in_privy:false,
  expires_at_unix:Math.floor(Date.now()/1000)+200,
  challenge:[
    'MiniNex - authorize an update to my EVM trading scope',
    'Wallet: '+wallet,'Previous policy SHA256: '+digest,
    'Requested policy SHA256: '+newDigest,
    'New or changed coins: QNT',
  ].join('\n'),
});
const extendConfirmed=()=>({
  status:'SIGNED_INTENT',wallet_signature_verified:true,
  policy_updated_in_privy:false,
});
const applied=()=>({
  status:'GRANT_VERIFIED',one_original_signer:true,new_coin:'QNT',
  additional_signer_created:false,remote_policy_verified:true,
  can_submit:false,
});

test('one first user coin creates one policy and asks Privy for only one addSigners grant',async()=>{
  const calls=[],signers=[];
  const paths=['/status','/prepare/QNT','/publish','/grant-offer','/confirm-grant','/status'];
  const reports=[
    scopeStatus([],'NOT_CONFIGURED'),
    draft(),published(),offer(),confirmed(),scopeStatus(['QNT']),
  ];
  let i=0;
  const request=async(path,method)=>{
    calls.push([path,method]);
    assert.equal(path,'/api/live/evm/wallet-policy'+paths[i]);
    return reports[i++];
  };
  const result=await authorizeCentralEvmWalletCoin(options(request,{
    addSigners:async data=>{signers.push(data);},
  }));
  assert.equal(result.status,'REMOTE_VERIFIED_READONLY');
  assert.equal(i,6);
  assert.equal(signers.length,1);
  assert.deepEqual(signers[0],{
    address:wallet,
    signers:[{signerId:signer,policyIds:[policyId]}],
  });
  assert.equal(calls.filter(x=>x[0].endsWith('/publish')).length,1);
});

test('second coin retains the same signer and requires one signMessage only',async()=>{
  const paths=['/status','/extend/QNT/prepare',
    '/extend/confirm','/extend/apply','/status'];
  const reports=[
    scopeStatus(['LINK']),challenge(),extendConfirmed(),
    applied(),scopeStatus(['LINK','QNT']),
  ];
  let i=0;
  let messages=0;
  const request=async(path,method,body)=>{
    assert.equal(path,'/api/live/evm/wallet-policy'+paths[i]);
    if(path.endsWith('/extend/confirm')){
      assert.deepEqual(body,{nonce,signature:'0x'+'f'.repeat(130)});
    }
    if(path.endsWith('/extend/apply')){
      assert.deepEqual(body,{nonce});
    }
    return reports[i++];
  };
  const final=await authorizeCentralEvmWalletCoin(options(request,{
    signMessage:async ({message},{address})=>{
      assert.match(message,/New or changed coins: QNT/);
      assert.equal(address,wallet);
      messages++;
      return {signature:'0x'+'f'.repeat(130)};
    },
  }));
  assert.equal(messages,1);
  assert.equal(i,5);
  assert.deepEqual(final.scope_coins,['LINK','QNT']);
});

test('already approved coin uses zero further wallet prompts',async()=>{
  let calls=0;
  const status=scopeStatus(['LINK','QNT']);
  const result=await authorizeCentralEvmWalletCoin(options(async()=>{
    calls++;
    return status;
  }));
  assert.equal(calls,1);
  assert.deepEqual(result,status);
});

test('never replay an uncertain or revoked original policy',async()=>{
  for(const state of ['SUBMITTING','UNCERTAIN','UPDATING',
    'UPDATE_UNCERTAIN','REVOKED','REQUIRES_RECONCILIATION']){
    let calls=0;
    await assert.rejects(()=>authorizeCentralEvmWalletCoin(options(async()=>{
      calls++;
      return scopeStatus(['LINK'],state);
    })),/reconciliation/);
    assert.equal(calls,1);
  }
});

test('new permission can never delete or replace a previously verified coin',async()=>{
  const states=[
    scopeStatus(['LINK']),challenge(),extendConfirmed(),
    applied(),scopeStatus(['QNT']),
  ];
  let i=0;
  await assert.rejects(()=>authorizeCentralEvmWalletCoin(options(
    async()=>states[i++],{
      signMessage:async()=>({signature:'0x'+'f'.repeat(130)}),
    },
  )),/wallet signer|policy changed/);
});

test('reject altered wallet, wrong fee terms and foreign policy signer',async()=>{
  for(const patch of [
    {wallet_address:'0x'+'b'.repeat(40)},
    {execution_signer_id:'not a signer ID'},
    {fee_scope:{...offer().fee_scope,performance_fee_percent:100}},
    {fee_scope:{...offer().fee_scope,treasury_wallet:'0x'+'c'.repeat(2)}},
  ]){
    const reports=[
      scopeStatus(['QNT'],'REMOTE_VERIFIED_UNATTACHED'),
      {...offer(),...patch},
    ];
    let i=0;
    await assert.rejects(()=>authorizeCentralEvmWalletCoin(options(
      async()=>reports[i++],{addSigners:async()=>{throw Error('Should not sign');}},
    )),/signing offer changed/);
    assert.equal(i,2);
  }
});

test('reject wrong source wallet or expired EIP-191 challenge without signing',async()=>{
  for(const c of [
    {...challenge(),challenge:'Other wallet approval'},
    {...challenge(),expires_at_unix:Math.floor(Date.now()/1000)-1},
    {...challenge(),target_policy_sha256:'garbage'},
  ]){
    let i=0;
    const stages=[scopeStatus(['LINK']),c];
    await assert.rejects(()=>authorizeCentralEvmWalletCoin(options(
      async()=>stages[i++],
    )),/challenge is stale/);
    assert.equal(i,2);
  }
});

test('refuse to count an EVM wallet policy as LIVE trading permission',()=>{
  assert.equal(centralWalletHasCoin(scopeStatus(['QNT']),'QNT'),true);
  assert.equal(centralWalletHasCoin(
    {...scopeStatus(['QNT']),auto_execution_enabled:true},'QNT',
  ),false);
  assert.equal(centralWalletHasCoin(
    {...scopeStatus(['QNT']),remote_policy_exactly_verified:false},'QNT',
  ),false);
  assert.equal(validCentralWalletStatus({
    ...scopeStatus(['QNT']),scope_coins:['QNT','QNT'],
  }),false);
});
