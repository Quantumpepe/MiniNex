import test from 'node:test';
import assert from 'node:assert/strict';
import {ownedSolanaWallet} from '../src/nativeCoinAddresses.js';
const address='11111111111111111111111111111111';
const owner={id:'did:privy:test',linkedAccounts:[{type:'wallet',chainType:'solana',walletClientType:'privy',address}]};
test('existing Solana Standard wallet matches the authenticated linked address',()=>{
  const w={address,standardWallet:{isPrivyWallet:true}};
  assert.equal(ownedSolanaWallet([w],address,owner),w);
  const sdk={address};
  assert.equal(ownedSolanaWallet([sdk],address,owner),sdk);
  assert.equal(ownedSolanaWallet([w],address,{id:'did:privy:other',linkedAccounts:[]}),null);
  assert.equal(ownedSolanaWallet([w,w],address,owner),null);
  assert.equal(ownedSolanaWallet([{address,walletClientType:'phantom'}],address,owner),null);
  assert.equal(ownedSolanaWallet([{address,standardWallet:{isPrivyWallet:false}}],address,owner),null);
});
