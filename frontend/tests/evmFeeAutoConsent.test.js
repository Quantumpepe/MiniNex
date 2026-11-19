import test from 'node:test';
import assert from 'node:assert/strict';
import {
  EVM_FEE_COINS,exactFeeCapAtomic,validFeeAutoChallenge,validFeeAutoGrantOffer,
} from '../src/evmFeeAutoConsentRules.js';

const WALLET='0x'+'ab'.repeat(20);
const TREASURY='0x'+'cd'.repeat(20);
const EXPIRES=Math.floor(Date.now()/1000)+86400;
const expected={asset:'LINK',stable:'USDC',address:WALLET,cap:'2500000'};

test('all six EVM fee consent coins are available independently',()=>{
  assert.deepEqual(EVM_FEE_COINS,['BNB','POL','AVAX','LINK','UNI','ARB']);
});

test('exact user-selected fee cap handles 6- and 18-decimal stablecoins',()=>{
  assert.equal(exactFeeCapAtomic('2.5','LINK','USDC'),'2500000');
  assert.equal(exactFeeCapAtomic('2.5','BNB','USDT'),'2500000000000000000');
  assert.equal(exactFeeCapAtomic('0.000001','UNI','USDC'),'1');
  assert.equal(exactFeeCapAtomic('0.000001','ARB','USDT'),'1');
  assert.equal(exactFeeCapAtomic('0.000000000000000001','BNB','USDT'),'1');
  assert.equal(exactFeeCapAtomic('1000000','LINK','USDC'),'1000000000000');
});

for(const text of ['0','-1','1e10','0.0000001','1,5','', '1.0000000000000000001']){
  test('invalid cap never silently rounds or widens: '+JSON.stringify(text),()=>{
    assert.equal(exactFeeCapAtomic(text,'LINK','USDC'),null);
  });
}
test('wrong stablecoin on a chain has no user consent cap',()=>{
  assert.equal(exactFeeCapAtomic('1','BNB','USDC'),null);
});

const challenge={
  mode:'EVM_OWNER_FEE_ENROLLMENT_INTENT_ONLY',
  asset:'LINK',stable:'USDC',chain_id:1,
  wallet_address:WALLET,treasury_wallet:TREASURY,
  owner_max_fee_atomic:'2500000',policy_sha256:'a'.repeat(64),
  message_to_sign:'Approve only the MiniNex five percent realized net-profit fee on this exact wallet and token',
  owner_wallet_eip191_signature_required:true,
  privy_grant_attached:false,fee_auto_transfer_enabled:false,
};

test('UI accepts exact fee consent challenge before wallet EIP191 signature',()=>{
  assert.equal(validFeeAutoChallenge(challenge,expected),true);
});
for(const [name,mutate] of [
  ['wrong wallet',c=>{c.wallet_address=TREASURY;}],
  ['wrong chain',c=>{c.chain_id=56;}],
  ['changed amount',c=>{c.owner_max_fee_atomic='3500000';}],
  ['not separate',c=>{c.privy_grant_attached=true;}],
  ['already sending',c=>{c.fee_auto_transfer_enabled=true;}],
  ['wrong treasury',c=>{c.treasury_wallet=WALLET;}],
]){
  test('UI rejects modified fee consent '+name,()=>{
    const copy=structuredClone(challenge);
    mutate(copy);
    assert.equal(validFeeAutoChallenge(copy,expected),false);
  });
}

const offer={
  mode:'OWNER_EVM_FEE_SIGNER_GRANT_OFFER_ONLY',
  asset:'LINK',stable:'USDC',chain_id:1,
  wallet_address:WALLET,treasury_wallet:TREASURY,
  owner_max_fee_atomic:'2500000',
  execution_signer_id:'signer_abcdefgh',
  policy_id:'policy_abcdefgh',
  policy_expires_unix:EXPIRES,owner_grant_required:true,
  privy_grant_attached:false,automatic_payment_sent:false,
};
const params={...expected,policyId:'policy_abcdefgh'};

test('UI recognizes exact Privy owner fee signer offer',()=>{
  assert.equal(validFeeAutoGrantOffer(offer,params),true);
});
for(const [name,mutate] of [
  ['wrong wallet',o=>{o.wallet_address=TREASURY;}],
  ['swapped policy',o=>{o.policy_id='policy_other1234';}],
  ['wrong signer',o=>{o.execution_signer_id='';}],
  ['wrong chain',o=>{o.chain_id=137;}],
  ['different approved cap',o=>{o.owner_max_fee_atomic='2000000';}],
  ['expired',o=>{o.policy_expires_unix=1;}],
  ['already attached',o=>{o.privy_grant_attached=true;}],
  ['fee already sent',o=>{o.automatic_payment_sent=true;}],
]){
  test('UI rejects changed Privy fee signer offer '+name,()=>{
    const altered=structuredClone(offer);
    mutate(altered);
    assert.equal(validFeeAutoGrantOffer(altered,params),false);
  });
}
