import test from 'node:test';
import assert from 'node:assert/strict';
import {validateOwnerRouter02Call,ROUTER02,STABLES,WETH9,abiApprove,
  abiAllowance,decodeUint,ownerAccounts} from '../src/ethLiveSwapRules.js';
const wallet='0x'+'a'.repeat(40);
const word=n=>BigInt(n).toString(16).padStart(64,'0');
const call=(side='BUY',stable='USDC')=>{
 const source=side==='BUY'?STABLES[stable]:WETH9;
 const dest=side==='BUY'?WETH9:STABLES[stable];
 return {wallet_identity_verified:true,unsigned_swap_validated:true,
  transaction_expires_at:Math.floor(Date.now()/1000)+12,
  transaction:{from:wallet,to:ROUTER02,chainId:1,value:'0x0',
    data:'0x04e45aaf'+[source,dest,500,wallet,
      side==='BUY'?10000000n:100000000000000000n,1,0].map(word).join('')}};
};
for(const side of ['BUY','SELL'])for(const stable of ['USDC','USDT']){
 test(side+' '+stable+' wallet transaction is executable ERC20/WETH',()=>{
  const result=validateOwnerRouter02Call(call(side,stable),{
   wallet,side,stable,amount:side==='BUY'?'10':'0.1'});
  assert.equal(result.to,ROUTER02);
  assert.equal(result.amountAtomic,side==='BUY'?10000000n:100000000000000000n);
 });
}
test('cannot use another wallet',()=>assert.throws(()=>validateOwnerRouter02Call(
 call(),{wallet:'0x'+'b'.repeat(40),side:'BUY',stable:'USDC',amount:'10'})));
test('cannot use expired quote',()=>{
 const q=call();q.transaction_expires_at=0;
 assert.throws(()=>validateOwnerRouter02Call(q,{wallet,side:'BUY',stable:'USDC',amount:'10'}));
});
test('cannot increase amount after quote',()=>assert.throws(()=>validateOwnerRouter02Call(
 call(),{wallet,side:'BUY',stable:'USDC',amount:'11'})));
test('cannot use foreign router',()=>{
 const q=call();q.transaction.to='0x'+'b'.repeat(40);
 assert.throws(()=>validateOwnerRouter02Call(q,{wallet,side:'BUY',stable:'USDC',amount:'10'}));
});
test('cannot replace token',()=>{
 const q=call();q.transaction.data=q.transaction.data.replace(
   STABLES.USDC.slice(2),STABLES.USDT.slice(2));
 assert.throws(()=>validateOwnerRouter02Call(q,{wallet,side:'BUY',stable:'USDC',amount:'10'}));
});
test('exact approval and ERC20 RPC inputs',()=>{
 assert.match(abiApprove(ROUTER02,10n),/^0x095ea7b3[0-9a-f]{128}$/);
 assert.match(abiAllowance(wallet,ROUTER02),/^0xdd62ed3e[0-9a-f]{128}$/);
 assert.equal(decodeUint('0x'+'0'.repeat(63)+'a'),10n);
 assert.deepEqual(ownerAccounts([wallet.toUpperCase()],wallet),true);
});

import {exactEthUnits,wethWithdrawCalldata} from '../src/ethLiveSwapRules.js';
test('native ETH conversion uses exact 18 decimals',()=>{
  assert.equal(exactEthUnits('0.1'),100000000000000000n);
  assert.equal(exactEthUnits('1.000000000000000001'),1000000000000000001n);
  assert.match(wethWithdrawCalldata(100000000000000000n),/^0x2e1a7d4d[0-9a-f]{64}$/);
  assert.throws(()=>exactEthUnits('0.0000000000000000001'));
});
