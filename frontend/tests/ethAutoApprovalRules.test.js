import test from 'node:test';
import assert from 'node:assert/strict';
import {validateEthAutoOwnerApproval,zeroAllowanceCalldata,
  AUTO_APPROVAL_TOKENS} from '../src/ethAutoApprovalRules.js';
import {ROUTER02} from '../src/ethLiveSwapRules.js';
const WALLET='0x'+'a'.repeat(40);
const ZERO='0'.repeat(64);
function plan(token='USDC'){
  const amount=token==='WETH'?'50000000000000000':'100000000';
  return {mode:'ETH_OWNER_LIMITED_ROUTER02_APPROVAL',asset:'ETH',
    network:'ethereum-mainnet',chain_id:1,
    wallet_address:WALLET,token,token_contract:AUTO_APPROVAL_TOKENS[token],
    spender:ROUTER02,amount_atomic:amount,
    approval_cap_usd:'100.000000',
    expires_at_unix:Math.floor(Date.now()/1000)+120,
    requires_explicit_owner_privy_confirmation:true,
    transaction_broadcast:false,backend_can_sign:false,
    transaction:{from:WALLET,to:AUTO_APPROVAL_TOKENS[token],
      chainId:1,value:'0x0',data:'0x095ea7b3'+
      ROUTER02.slice(2).padStart(64,'0')+
      BigInt(amount).toString(16).padStart(64,'0')},
  };
}
for(const asset of ['USDC','USDT','WETH']){
  test(asset+' bound owner approval',()=>{
    const tx=validateEthAutoOwnerApproval(plan(asset),{wallet:WALLET,token:asset});
    assert.equal(tx.amount,BigInt(plan(asset).amount_atomic));
    assert.equal(tx.spender,ROUTER02);
  });
}
test('token substitution is rejected',()=>{
  const p=plan();p.transaction.to=AUTO_APPROVAL_TOKENS.USDT;
  assert.throws(()=>validateEthAutoOwnerApproval(p,{wallet:WALLET,token:'USDC'}));
});
test('another wallet is rejected',()=>assert.throws(()=>
  validateEthAutoOwnerApproval(plan(),{
    wallet:'0x'+'b'.repeat(40),token:'USDC'})));
test('unlimited allowance substitution is rejected',()=>{
  const p=plan();p.amount_atomic=String(2n**256n-1n);
  assert.throws(()=>validateEthAutoOwnerApproval(p,{wallet:WALLET,token:'USDC'}));
});
test('expired authorization cannot be signed',()=>{
  const p=plan();p.expires_at_unix=1;
  assert.throws(()=>validateEthAutoOwnerApproval(p,{wallet:WALLET,token:'USDC'}));
});
test('USDT nonzero allowance needs exact zero-reset calldata',()=>{
  assert.equal(zeroAllowanceCalldata(),
    '0x095ea7b3'+ROUTER02.slice(2).padStart(64,'0')+ZERO);
});
