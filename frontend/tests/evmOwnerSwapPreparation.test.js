import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validateOwnerSwapPreparation,exactNativeOwnerUnits,OWNER_SWAP_CHAINS,
} from '../src/evmOwnerSwapPreparation.js';

const WALLET='0x'+'ab'.repeat(20);
const ROUTER='0x'+'cd'.repeat(20);
const STABLE='0x'+'11'.repeat(20);
const TOKEN='0x'+'22'.repeat(20);
const word=value=>BigInt(value).toString(16).padStart(64,'0');
function quote(asset='QNT',side='BUY',stable='USDC',amount='1'){
  const data='0x04e45aaf'+[
    STABLE,TOKEN,3000,WALLET,10n**6n,10n**18n,0n].map(word).join('');
  return {
    mode:'SHARED_EVM_OWNER_SWAP_PREPARATION',
    asset,side,stable,input_amount:amount,chain_id:OWNER_SWAP_CHAINS[asset],
    wallet_identity_verified:true,exact_swap_abi_and_amount_verified:true,
    wallet_address:WALLET,router:ROUTER,
    source_token_address:STABLE,output_token_address:TOKEN,
    execution_enabled:false,can_submit:false,
    requires_separate_user_signature:true,
    source_funds_sufficient:true,router_allowance_sufficient:true,
    native_gas_available:true,
    owner_wallet_signing_possible:true,
    unsigned_transaction:{chainId:OWNER_SWAP_CHAINS[asset],from:WALLET,to:ROUTER,
      data,value:'0x0'},
    transaction_expires_at:Math.floor(Date.now()/1000)+35,
    owner_approval_transaction:null,
  };
}
const check=(data,asset='QNT',side='BUY',stable='USDC',amount='1')=>
  validateOwnerSwapPreparation(data,{asset,side,stable,amount,wallet:WALLET});

test('owner-verified V3 swap matches exact wallet and nonzero min output',()=>{
  const p=check(quote());
  assert.equal(p.swap.minimum,10n**18n);
  assert.equal(p.swap.quantity,10n**6n);
  assert.equal(p.approve,null);
  assert.equal(p.chainId,1);
});

test('other chain, wallet, asset, amount and zero min output cannot sign',()=>{
  const changes=[
    q=>q.unsigned_transaction.chainId=56,
    q=>q.chain_id=42161,
    q=>q.wallet_address='0x'+'ff'.repeat(20),
    q=>q.unsigned_transaction.from='0x'+'ff'.repeat(20),
    q=>q.unsigned_transaction.data='0x04e45aaf'+[
      STABLE,TOKEN,3000,WALLET,10n**6n,0n,0n].map(word).join(''),
    q=>q.unsigned_transaction.to='0x'+'ff'.repeat(20),
    q=>q.unsigned_transaction.value='0x1',
    q=>q.transaction_expires_at=1,
    q=>q.input_amount='1000',
    q=>q.can_submit=true,
    q=>q.requires_separate_user_signature=false,
  ];
  for(const change of changes){
    const q=quote();
    change(q);
    assert.throws(()=>check(q));
  }
});

test('approval checks exact token, router and finite approved amount',()=>{
  const q=quote();
  q.unsigned_transaction=null;
  q.owner_wallet_signing_possible=false;
  q.router_allowance_sufficient=false;
  q.owner_approval_transaction={
    chainId:1,from:WALLET,to:STABLE,value:'0x0',
    data:'0x095ea7b3'+word(ROUTER)+word(10n**6n),
  };
  assert.equal(check(q).approve.amount,10n**6n);
  q.owner_approval_transaction.data='0x095ea7b3'+word(ROUTER)+word(2n**256n-1n);
  assert.throws(()=>check(q),/unlimited/);
  q.owner_approval_transaction.data='0x095ea7b3'+word(TOKEN)+word(10n**6n);
  assert.throws(()=>check(q),/Invalid/);
});

test('native coin units preserve precision and reject invalid quantities',()=>{
  assert.equal(exactNativeOwnerUnits('0.000000000000000001'),1n);
  assert.equal(exactNativeOwnerUnits('1.23'),1230000000000000000n);
  for(const v of ['0','-1','1e10','0.0000000000000000001','NaN','','.1']){
    assert.throws(()=>exactNativeOwnerUnits(v));
  }
});
